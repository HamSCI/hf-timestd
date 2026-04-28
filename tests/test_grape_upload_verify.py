"""Tests for SFTPUpload.verify — the post-upload integrity gate.

These tests pin down the contract that gates the post-upload cleanup
branch in `cli.py grape daily` (which deletes the local decimated
data and the upload package on success).  Before this test suite,
``verify()`` was a hardcoded ``return True`` and the entire silent-
data-loss path was unguarded; the assertions here lock down the
"verify must agree with the remote server" behaviour so it can't
silently regress.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from hf_timestd.grape import uploader  # noqa: E402


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------

class BuildRemoteManifestTests(unittest.TestCase):
    def test_walks_dataset_recursively_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "OBS2025-04-28T00-00"
            (local / "S000171" / "OBSERVATORYMETADATA").mkdir(parents=True)
            (local / "S000171" / "INSTRUMENTMETADATA").mkdir()
            (local / "S000171" / "RX-888-DATA" / "ch0" / "tx0").mkdir(parents=True)

            (local / "drf_properties.h5").write_bytes(b"a" * 1024)
            (local / "S000171" / "OBSERVATORYMETADATA" / "obs.csv").write_bytes(b"x" * 200)
            (local / "S000171" / "INSTRUMENTMETADATA" / "inst.csv").write_bytes(b"y" * 250)
            (local / "S000171" / "RX-888-DATA" / "ch0" / "tx0" / "0001.h5").write_bytes(b"z" * 4096)

            manifest = uploader._build_remote_manifest(local, local.name)

        # Each entry is ('<dataset_name>/<rel>/<file>', size).  All paths
        # carry the dataset_name as their first component.
        for path, _ in manifest:
            self.assertTrue(path.startswith(local.name + "/"))

        sizes_by_path = dict(manifest)
        self.assertEqual(
            sizes_by_path[f"{local.name}/drf_properties.h5"], 1024)
        self.assertEqual(
            sizes_by_path[f"{local.name}/S000171/OBSERVATORYMETADATA/obs.csv"],
            200)
        self.assertEqual(
            sizes_by_path[f"{local.name}/S000171/INSTRUMENTMETADATA/inst.csv"],
            250)
        self.assertEqual(
            sizes_by_path[f"{local.name}/S000171/RX-888-DATA/ch0/tx0/0001.h5"],
            4096)
        self.assertEqual(len(manifest), 4)

    def test_empty_dataset_yields_empty_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "EMPTY"
            local.mkdir()
            manifest = uploader._build_remote_manifest(local, local.name)
        self.assertEqual(manifest, [])


# ---------------------------------------------------------------------------
# ls -l size parser
# ---------------------------------------------------------------------------

class ParseSftpLsSizesTests(unittest.TestCase):
    def test_extracts_sizes_in_order(self):
        out = (
            "-rw-r--r--    1 user group     1024 Apr 28 12:00 a.h5\n"
            "-rw-r--r--    1 user group  9876543 Apr 28 12:00 b.h5\n"
            "-rw-r--r--    1 user group        0 Apr 28 12:00 empty\n"
        )
        self.assertEqual(uploader._parse_sftp_ls_sizes(out),
                         [1024, 9876543, 0])

    def test_ignores_directory_lines_from_ls_d(self):
        out = (
            "-rw-r--r--    1 user group     1024 Apr 28 12:00 file.h5\n"
            "drwxr-xr-x    2 user group     4096 Apr 28 12:00 trigger_dir\n"
        )
        # ls -d still produces a "d..." line — we keep it in the size list
        # because it matches the long-listing pattern; the caller checks the
        # trigger dir presence separately by name search, not by size.
        sizes = uploader._parse_sftp_ls_sizes(out)
        self.assertEqual(sizes, [1024, 4096])

    def test_ignores_banner_and_blank_lines(self):
        out = (
            "Connected to host.\n"
            "\n"
            "-rw-r--r--    1 user group  1234 Apr 28 12:00 only.h5\n"
            "sftp> quit\n"
        )
        self.assertEqual(uploader._parse_sftp_ls_sizes(out), [1234])

    def test_handles_empty_input(self):
        self.assertEqual(uploader._parse_sftp_ls_sizes(""), [])
        self.assertEqual(uploader._parse_sftp_ls_sizes(None), [])


# ---------------------------------------------------------------------------
# SFTPUpload.verify integration (subprocess mocked)
# ---------------------------------------------------------------------------

def _make_uploader(tmpdir):
    return uploader.SFTPUpload({
        "host": "psws.test",
        "user": "S000171",
        "ssh": {"key_file": tmpdir / "key"},
    })


def _seed_dataset(d: Path) -> tuple[Path, str, str]:
    """Build a tiny realistic Digital RF-shaped tree and stash the upload
    context the way upload() does — so verify() has something to ask about.
    """
    local = d / "OBS2025-04-28T00-00"
    (local / "S000171" / "ch0").mkdir(parents=True)
    (local / "drf.h5").write_bytes(b"a" * 100)
    (local / "S000171" / "ch0" / "data.h5").write_bytes(b"b" * 500)
    return local, local.name, f"c{local.name}_#172_#STAMP"


def _stub_subprocess_run(stdout: str, stderr: str = "",
                         returncode: int = 0):
    """Build a fake subprocess.run that returns the given streams."""
    def _runner(cmd, *args, **kw):
        return SimpleNamespace(
            stdout=stdout, stderr=stderr, returncode=returncode,
        )
    return _runner


class VerifyContractTests(unittest.TestCase):
    def test_refuses_without_prior_upload_context(self):
        with tempfile.TemporaryDirectory() as d:
            up = _make_uploader(Path(d))
            self.assertFalse(up.verify("anywhere"))

    def test_returns_true_when_remote_matches_local(self):
        with tempfile.TemporaryDirectory() as d:
            up = _make_uploader(Path(d))
            local, dataset_name, trigger = _seed_dataset(Path(d))
            up._last_upload = {
                'local_path':   local,
                'dataset_name': dataset_name,
                'trigger_dir':  trigger,
            }
            # Walk order is deterministic: root files (drf.h5, 100)
            # come before subdir files (S000171/ch0/data.h5, 500),
            # then ls -d for the trigger directory.
            stdout = (
                f"-rw-r--r-- 1 u g  100 Apr 28 12:00 drf.h5\n"
                f"-rw-r--r-- 1 u g  500 Apr 28 12:00 data.h5\n"
                f"drwxr-xr-x 2 u g 4096 Apr 28 12:00 {trigger}\n"
            )
            with mock.patch("subprocess.run",
                            _stub_subprocess_run(stdout)):
                self.assertTrue(up.verify(""))

    def test_returns_false_on_size_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            up = _make_uploader(Path(d))
            local, dataset_name, trigger = _seed_dataset(Path(d))
            up._last_upload = {
                'local_path':   local,
                'dataset_name': dataset_name,
                'trigger_dir':  trigger,
            }
            # Simulate a wire truncation: server received 50 bytes
            # of what should have been 100.  drf.h5 comes first in
            # walk order, so that's the one we corrupt here.
            stdout = (
                f"-rw-r--r-- 1 u g   50 Apr 28 12:00 drf.h5\n"   # truncated!
                f"-rw-r--r-- 1 u g  500 Apr 28 12:00 data.h5\n"
                f"drwxr-xr-x 2 u g 4096 Apr 28 12:00 {trigger}\n"
            )
            with mock.patch("subprocess.run",
                            _stub_subprocess_run(stdout)):
                self.assertFalse(up.verify(""))

    def test_returns_false_when_remote_reports_missing(self):
        with tempfile.TemporaryDirectory() as d:
            up = _make_uploader(Path(d))
            local, dataset_name, trigger = _seed_dataset(Path(d))
            up._last_upload = {
                'local_path':   local,
                'dataset_name': dataset_name,
                'trigger_dir':  trigger,
            }
            stderr = "Can't ls: \"OBS2025-04-28T00-00/drf.h5\" not found\n"
            with mock.patch("subprocess.run",
                            _stub_subprocess_run(stdout="", stderr=stderr)):
                self.assertFalse(up.verify(""))

    def test_returns_false_when_trigger_dir_absent(self):
        with tempfile.TemporaryDirectory() as d:
            up = _make_uploader(Path(d))
            local, dataset_name, trigger = _seed_dataset(Path(d))
            up._last_upload = {
                'local_path':   local,
                'dataset_name': dataset_name,
                'trigger_dir':  trigger,
            }
            # Sizes match but the trigger directory's ls -d output is
            # absent (e.g. the `mkdir <trigger>` failed on the server).
            stdout = (
                f"-rw-r--r-- 1 u g  100 Apr 28 12:00 drf.h5\n"
                f"-rw-r--r-- 1 u g  500 Apr 28 12:00 data.h5\n"
                # No trigger line.
            )
            with mock.patch("subprocess.run",
                            _stub_subprocess_run(stdout)):
                self.assertFalse(up.verify(""))

    def test_returns_false_on_sftp_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            up = _make_uploader(Path(d))
            local, dataset_name, trigger = _seed_dataset(Path(d))
            up._last_upload = {
                'local_path':   local,
                'dataset_name': dataset_name,
                'trigger_dir':  trigger,
            }
            import subprocess as _sp

            def _explode(*a, **kw):
                raise _sp.TimeoutExpired(cmd="sftp", timeout=1)

            with mock.patch("subprocess.run", _explode):
                self.assertFalse(up.verify(""))

    def test_upload_records_context_for_verify(self):
        """Upload success must stash exactly the keys verify() expects."""
        with tempfile.TemporaryDirectory() as d:
            up = _make_uploader(Path(d))
            local, _, _ = _seed_dataset(Path(d))
            with mock.patch("subprocess.run",
                            _stub_subprocess_run(stdout="", returncode=0)):
                ok = up.upload(local, remote_path="",
                               metadata={"instrument_id": "172"})
            self.assertTrue(ok)
            self.assertIsNotNone(up._last_upload)
            self.assertEqual(up._last_upload['local_path'], local)
            self.assertEqual(up._last_upload['dataset_name'], local.name)
            self.assertTrue(
                up._last_upload['trigger_dir'].startswith(f"c{local.name}_#172_#"))

    def test_upload_failure_does_not_set_context(self):
        """If sftp put exits non-zero, _last_upload must stay unset so
        verify() can't accidentally claim success on the previous run."""
        with tempfile.TemporaryDirectory() as d:
            up = _make_uploader(Path(d))
            local, _, _ = _seed_dataset(Path(d))
            with mock.patch("subprocess.run",
                            _stub_subprocess_run(stdout="", stderr="boom",
                                                 returncode=1)):
                ok = up.upload(local, remote_path="",
                               metadata={"instrument_id": "172"})
            self.assertFalse(ok)
            self.assertIsNone(up._last_upload)


if __name__ == "__main__":
    unittest.main()
