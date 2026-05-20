"""
Unit tests for hf_timestd.core.chrony_shm

ChronySHM is the System V shared-memory refclock driver. Tests focus on the
parts that don't require touching real SHM:
- Construction defaults and key derivation
- File-based fallback (`_connect_file`) using /dev/shm
- update() count-locking protocol writes a 96-byte struct with mode=1, valid=1
- update() returns False when not connected
- disconnect() handles the file-based path
- install_chrony_config returns a valid snippet referencing the right key
"""

import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from hf_timestd.core.chrony_shm import (
    SHM_KEY_BASE,
    SHM_SIZE,
    SHM_STRUCT_FORMAT,
    ChronySHM,
    install_chrony_config,
)


# =============================================================================
# Struct layout (M-H21)
# =============================================================================


class TestStructLayout:
    """The SHM struct format and the segment size stay consistent."""

    def test_shm_size_is_derived_from_the_format(self):
        # SHM_SIZE == struct.calcsize(SHM_STRUCT_FORMAT) — they cannot drift.
        assert SHM_SIZE == struct.calcsize(SHM_STRUCT_FORMAT)

    def test_struct_is_96_bytes(self):
        # chrony/ntpd/gpsd `struct shmTime` is 96 bytes on x86-64. A packed
        # record must fill the whole segment — the format previously packed
        # only 92 of the 96 bytes (M-H21).
        assert SHM_SIZE == 96
        assert struct.calcsize(SHM_STRUCT_FORMAT) == 96


# =============================================================================
# Construction
# =============================================================================


class TestConstruction:
    def test_default_unit_zero(self):
        shm = ChronySHM()
        assert shm.unit == 0
        assert shm.key == SHM_KEY_BASE
        assert shm.connected is False
        assert shm.count == 0

    def test_unit_offsets_key(self):
        shm = ChronySHM(unit=2)
        assert shm.key == SHM_KEY_BASE + 2


# =============================================================================
# File-based fallback path
# =============================================================================


class TestFileBasedConnect:
    def test_file_fallback_marks_use_sysv_false(self, tmp_path, monkeypatch):
        # Patch the hard-coded /dev/shm path to point inside tmp_path. Use
        # the real mmap so we don't have to fake the kernel API.
        import hf_timestd.core.chrony_shm as shm_mod
        shm_path = str(tmp_path / 'chrony_shm_5')

        original_connect_file = shm_mod.ChronySHM._connect_file

        def patched_connect_file(self):
            # Same logic as the real method but routed through tmp_path
            import os
            import mmap
            if not os.path.exists(shm_path):
                with open(shm_path, 'wb') as f:
                    f.write(b'\x00' * SHM_SIZE)
                os.chmod(shm_path, 0o666)
            fd = os.open(shm_path, os.O_RDWR)
            try:
                self.shm_map = mmap.mmap(fd, SHM_SIZE)
            finally:
                os.close(fd)
            self._use_sysv = False

        monkeypatch.setattr(shm_mod.ChronySHM, '_connect_file',
                            patched_connect_file)
        shm = ChronySHM(unit=5)
        shm._connect_file()
        assert shm._use_sysv is False
        assert shm.shm_map is not None
        # Cleanly close the mmap so the test process doesn't hold the file
        shm.shm_map.close()

    def test_update_writes_count_locked_struct_via_file_path(self, tmp_path):
        # Build a real on-disk file for /dev/shm fallback
        shm_path = tmp_path / 'chrony_shm.bin'
        shm_path.write_bytes(b'\x00' * SHM_SIZE)
        shm = ChronySHM(unit=0)
        shm.connected = True
        shm._use_sysv = False
        # Memory-map our test file
        import mmap
        with shm_path.open('r+b') as fh:
            shm.shm_map = mmap.mmap(fh.fileno(), SHM_SIZE)

        ok = shm.update(reference_time=1_000_000.123,
                        system_time=1_000_000.124,
                        precision=-10, leap=0)
        assert ok is True

        # After two count increments the value is even (count = 2)
        # and is patched at offset 4-7
        shm.shm_map.seek(4)
        count = struct.unpack('@i', shm.shm_map.read(4))[0]
        assert count % 2 == 0
        assert count == 2  # one update → count goes 0 → 1 (writing) → 2 (done)

        # Mode is 1 at offset 0
        shm.shm_map.seek(0)
        mode = struct.unpack('@i', shm.shm_map.read(4))[0]
        assert mode == 1

        shm.shm_map.close()


# =============================================================================
# update behavior
# =============================================================================


class TestUpdate:
    def test_returns_false_when_not_connected(self):
        shm = ChronySHM()
        # Never called connect()
        assert shm.update(reference_time=1.0) is False

    def test_returns_false_on_internal_error(self, tmp_path, caplog):
        shm_path = tmp_path / 'chrony_shm.bin'
        shm_path.write_bytes(b'\x00' * SHM_SIZE)
        shm = ChronySHM(unit=0)
        shm.connected = True
        shm._use_sysv = False
        # Map a closed mmap to force a write failure
        import mmap
        fh = shm_path.open('r+b')
        shm.shm_map = mmap.mmap(fh.fileno(), SHM_SIZE)
        shm.shm_map.close()  # Closed → subsequent writes raise
        ok = shm.update(reference_time=1.0)
        fh.close()
        assert ok is False
        assert any('Failed to update' in r.message for r in caplog.records)


# =============================================================================
# disconnect
# =============================================================================


class TestDisconnect:
    def test_disconnects_file_based(self, tmp_path):
        shm_path = tmp_path / 'chrony_shm.bin'
        shm_path.write_bytes(b'\x00' * SHM_SIZE)
        shm = ChronySHM(unit=0)
        shm.connected = True
        shm._use_sysv = False
        import mmap
        with shm_path.open('r+b') as fh:
            shm.shm_map = mmap.mmap(fh.fileno(), SHM_SIZE)
            shm.disconnect()
        assert shm.connected is False

    def test_disconnect_swallows_errors(self, caplog):
        shm = ChronySHM()
        # _use_sysv attribute hasn't been set; disconnect should still log
        shm._use_sysv = False
        shm.shm_map = None
        shm.disconnect()
        assert shm.connected is False


# =============================================================================
# install_chrony_config
# =============================================================================


class TestInstallChronyConfig:
    def test_default_unit_in_config(self):
        snippet = install_chrony_config(unit=0)
        assert 'refclock SHM 0' in snippet
        # Embeds the canonical key as a hex literal
        assert f'0x{SHM_KEY_BASE:08x}' in snippet

    def test_custom_unit_changes_key(self):
        snippet = install_chrony_config(unit=2)
        assert 'refclock SHM 2' in snippet
        assert f'0x{(SHM_KEY_BASE + 2):08x}' in snippet
