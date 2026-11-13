"""timestd-prune only deletes from the SSD now: the archiver copies to the
archive drive (sigmond archive-drive design 2026-11-12, 'Retiring the old move')."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_prune_unit_has_no_archive_root():
    assert "--archive-root" not in (ROOT / "systemd" / "timestd-prune.service").read_text()


def test_raw_cleanup_has_no_archive_root():
    text = (ROOT / "deploy" / "systemd" / "timestd-raw-cleanup.service").read_text()
    assert "/mnt/timestd-archive" not in text and "mv " not in text
