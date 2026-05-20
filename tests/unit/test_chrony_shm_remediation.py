#!/usr/bin/env python3
"""
Unit tests for the M-M16 / M-M17 remediation in ``chrony_shm.py``.

M-M16 — fix permissions in place or fail loudly
  The previous "recovery" path detached, ``self.shm.remove()`` (=
  ``shmctl(IPC_RMID)``), and recreated.  But IPC_RMID only marks a SysV
  segment for deletion; chronyd stays attached to the old shmid forever,
  while we write to a new, unread one.  The fix changes the mode in
  place via ``self.shm.mode = 0o666`` (shmctl(IPC_SET)) — or raises.

M-M17 — failed update clears `.connected` + escalates on streak
  The previous ``update()`` returned False on exception but left
  ``self.connected`` True, so the caller's reconnect path
  (``multi_broadcast_fusion._loop``'s rate-limited reconnect block)
  never fired.  Now a failed update marks disconnected and bumps a
  consecutive-failure counter; the counter logs at CRITICAL once it
  reaches the escalation threshold.
"""

import struct
import unittest
from unittest.mock import patch

import pytest

from hf_timestd.core.chrony_shm import SHM_SIZE, ChronySHM


# ---------------------------------------------------------------------
# M-M16: in-place permission fix vs fail-loudly
# ---------------------------------------------------------------------

class _FakeSysVIPCSegment:
    """In-process stand-in for sysv_ipc.SharedMemory.  Mode is
    writable via assignment; assignment can be configured to raise to
    simulate lacking CAP_IPC_OWNER."""

    def __init__(self, *, key, mode=0o600, uid=0,
                 allow_mode_change=True):
        self.key = key
        self._mode = mode
        self.uid = uid
        self._allow_mode_change = allow_mode_change
        self.detached = False
        self.removed = False

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, new_mode):
        if not self._allow_mode_change:
            raise PermissionError(
                "simulated: lacking CAP_IPC_OWNER for shmctl(IPC_SET)"
            )
        self._mode = new_mode

    def detach(self):
        self.detached = True

    def remove(self):
        self.removed = True

    def write(self, data, offset):
        pass


class _FakeSysVIPCModule:
    """Just the bits of the sysv_ipc surface that `_connect_sysv` uses."""

    IPC_CREAT = 0o1000
    IPC_EXCL = 0o2000

    class ExistentialError(Exception):
        pass

    class PermissionsError(PermissionError):
        pass

    def __init__(self, *, segment_factory):
        self._segment_factory = segment_factory
        self._create_calls = 0
        self._attach_calls = 0

    def SharedMemory(self, key, *, flags, size, mode=0o600):
        # IPC_CREAT|IPC_EXCL → "create-exclusive"; succeed when the
        # factory has no existing segment, else ExistentialError.
        if flags & self.IPC_EXCL:
            self._create_calls += 1
            seg = self._segment_factory(key=key, mode=mode, exists=False)
            if seg is None:
                raise self.ExistentialError("simulated: segment exists")
            return seg
        # Plain attach (flags=0).
        self._attach_calls += 1
        seg = self._segment_factory(key=key, mode=mode, exists=True)
        if seg is None:
            raise self.PermissionsError("simulated: cannot attach")
        return seg


class TestConnectSysvPermissionsInPlace(unittest.TestCase):
    def test_fixes_mode_in_place_when_allowed(self):
        """When IPC_SET is permitted, we mutate the existing segment —
        no detach, no remove."""
        existing = _FakeSysVIPCSegment(
            key=0x4e545030, mode=0o600, uid=0, allow_mode_change=True,
        )

        def factory(*, key, mode, exists):
            return existing if exists else None  # IPC_EXCL fails → exists

        fake = _FakeSysVIPCModule(segment_factory=factory)
        shm = ChronySHM(unit=0)
        shm._connect_sysv(fake)

        self.assertIs(shm.shm, existing)
        self.assertEqual(existing.mode, 0o666)
        self.assertFalse(existing.detached)
        self.assertFalse(existing.removed)
        self.assertTrue(shm._use_sysv)

    def test_attach_succeeds_with_good_mode_uses_segment_as_is(self):
        existing = _FakeSysVIPCSegment(
            key=0x4e545030, mode=0o666, uid=0, allow_mode_change=False,
        )

        def factory(*, key, mode, exists):
            return existing if exists else None

        fake = _FakeSysVIPCModule(segment_factory=factory)
        shm = ChronySHM(unit=0)
        shm._connect_sysv(fake)

        # No need to touch mode — already 0o666.
        self.assertEqual(existing.mode, 0o666)
        self.assertFalse(existing.detached)
        self.assertFalse(existing.removed)

    def test_fails_loudly_when_mode_change_denied(self):
        """The old code would `detach + remove + recreate`, silently
        orphaning chronyd.  Now we raise."""
        existing = _FakeSysVIPCSegment(
            key=0x4e545030, mode=0o600, uid=0, allow_mode_change=False,
        )

        def factory(*, key, mode, exists):
            return existing if exists else None

        fake = _FakeSysVIPCModule(segment_factory=factory)
        shm = ChronySHM(unit=0)

        with self.assertRaises(PermissionError) as cm:
            shm._connect_sysv(fake)

        # The error message must point operators at the right recovery.
        msg = str(cm.exception)
        self.assertIn("CAP_IPC_OWNER", msg)
        self.assertIn("ipcrm -M", msg)
        # Crucially, the segment was NOT removed and NOT recreated.
        self.assertFalse(existing.removed)
        # We do detach to release our handle before raising.
        self.assertTrue(existing.detached)

    def test_fails_loudly_when_cannot_attach(self):
        """Even when we can't attach at all, no automatic ipcrm — fail
        loudly so the operator must decide."""

        def factory(*, key, mode, exists):
            return None  # always fail (both IPC_EXCL and attach)

        fake = _FakeSysVIPCModule(segment_factory=factory)
        shm = ChronySHM(unit=0)

        with self.assertRaises(PermissionError) as cm:
            shm._connect_sysv(fake)

        msg = str(cm.exception)
        self.assertIn("permission denied", msg.lower())
        self.assertIn("ipcrm -M", msg)


# ---------------------------------------------------------------------
# M-M17: update() clears `.connected` and escalates streaks
# ---------------------------------------------------------------------

class TestUpdateFailureDisconnects(unittest.TestCase):
    def _file_backed_shm(self, tmp_path, *, bad_mmap=False):
        shm_path = tmp_path / "chrony_shm.bin"
        shm_path.write_bytes(b"\x00" * SHM_SIZE)
        shm = ChronySHM(unit=0)
        shm.connected = True
        shm._use_sysv = False
        import mmap
        fh = shm_path.open("r+b")
        m = mmap.mmap(fh.fileno(), SHM_SIZE)
        if bad_mmap:
            m.close()  # subsequent writes raise
        shm.shm_map = m
        return shm, fh

    def test_failed_update_clears_connected(self, tmp_path=None):
        # pytest-style tmp_path injection through unittest is awkward;
        # use a manual TemporaryDirectory.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            shm, fh = self._file_backed_shm(Path(td), bad_mmap=True)
            try:
                self.assertTrue(shm.connected)
                ok = shm.update(reference_time=1.0)
                self.assertFalse(ok)
                # M-M17 invariant: a failed update clears `connected`
                # so the caller's reconnect path fires next cycle.
                self.assertFalse(shm.connected)
                self.assertEqual(shm._consecutive_update_failures, 1)
            finally:
                fh.close()

    def test_streak_escalates_at_threshold(self, tmp_path=None):
        import logging
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            shm, fh = self._file_backed_shm(Path(td), bad_mmap=True)
            try:
                # Drive the counter past the escalation threshold by
                # re-marking connected each iteration (the caller's
                # reconnect would normally do this; here we simulate
                # without actually reconnecting).
                with self.assertLogs("hf_timestd", level="CRITICAL") as cm:
                    for _ in range(shm._UPDATE_FAILURE_ESCALATE_AT):
                        shm.connected = True
                        shm.update(reference_time=1.0)
                # At least one CRITICAL log emitted from the streak.
                self.assertTrue(any(
                    "consecutive update failures" in rec.message
                    and rec.levelno == logging.CRITICAL
                    for rec in cm.records
                ))
                self.assertGreaterEqual(
                    shm._consecutive_update_failures,
                    shm._UPDATE_FAILURE_ESCALATE_AT,
                )
            finally:
                fh.close()

    def test_successful_update_resets_streak(self, tmp_path=None):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            # Real (working) mmap so the first update succeeds.
            shm, fh = self._file_backed_shm(Path(td), bad_mmap=False)
            try:
                shm._consecutive_update_failures = 3  # pre-seed
                ok = shm.update(reference_time=1.0)
                self.assertTrue(ok)
                self.assertEqual(shm._consecutive_update_failures, 0)
            finally:
                fh.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
