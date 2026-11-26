"""The raw-cleanup unit never moves files to the archive drive; the archiver copies."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_raw_cleanup_has_no_archive_root():
    text = (ROOT / "deploy" / "systemd" / "timestd-raw-cleanup.service").read_text()
    assert "/mnt/timestd-archive" not in text and "mv " not in text
