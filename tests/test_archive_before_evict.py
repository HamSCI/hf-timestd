"""archive-before-evict (ledger, 2026-11-14): with an archive drive configured, no
raw day leaves the SSD before every chunk of it is on the drive, unless the disk
is past the emergency mark.  KPH2 lost raw_buffer/20261112 on 11-14 when the
resource guardian evicted it while the archiver was blocked."""
import json
from pathlib import Path

import pytest

from hf_timestd import archiver as A
from hf_timestd.core.resource_guardian import ResourceGuardian
from hf_timestd.quota_manager import QuotaManager

DAY = "20261112"


@pytest.fixture
def site(tmp_path, monkeypatch):
    data = tmp_path / "ssd"
    mount = tmp_path / "mnt"
    for ch in ("A", "B"):
        d = data / "raw_buffer" / ch / DAY
        d.mkdir(parents=True)
        for s in (1, 2):
            (d / f"{s}.bin.zst").write_bytes(b"\0" * 4096)
            (d / f"{s}.json").write_text("{}")
    mount.mkdir()
    toml = tmp_path / "archive.toml"
    toml.write_text(f'uuid = "u"\nmount = "{mount}"\n')
    monkeypatch.setattr(A, "ARCHIVE_TOML", toml)
    monkeypatch.setattr(A, "_is_mount", lambda p: True)
    monkeypatch.setattr(A, "_uuid_matches", lambda p, u: True)
    monkeypatch.setattr(A, "_station", lambda: "H")
    return data, mount / "timestd" / "H"


def _archive(drive_root, chans=("A", "B"), stems=(1, 2)):
    for ch in chans:
        d = drive_root / "raw_buffer" / ch / DAY
        d.mkdir(parents=True, exist_ok=True)
        for s in stems:
            (d / f"{s}.bin.zst").write_bytes(b"x")
            (d / f"{s}.json").write_text("{}")


def test_not_configured_means_free_to_evict(site, monkeypatch, tmp_path):
    data, _ = site
    monkeypatch.setattr(A, "ARCHIVE_TOML", tmp_path / "none.toml")
    assert A.raw_day_may_leave(data, DAY, usage_percent=50) is True


def test_unarchived_day_stays(site):
    data, drive = site
    _archive(drive, chans=("A",))
    assert A.raw_day_archived(data, DAY) is False
    assert A.raw_day_may_leave(data, DAY, usage_percent=90) is False


def test_fully_archived_day_may_go(site):
    data, drive = site
    _archive(drive)
    assert A.raw_day_archived(data, DAY) is True
    assert A.raw_day_may_leave(data, DAY, usage_percent=90) is True


def test_emergency_lets_it_go(site):
    data, _ = site
    assert A.raw_day_may_leave(data, DAY, usage_percent=A.EVICT_EMERGENCY_PERCENT) is True


def test_drive_away_means_not_archived(site, monkeypatch):
    data, drive = site
    _archive(drive)
    monkeypatch.setattr(A, "_is_mount", lambda p: False)
    assert A.raw_day_archived(data, DAY) is False


def test_quota_manager_skips_unarchived_raw_day(site):
    data, drive = site
    qm = QuotaManager(data_root=data, threshold_percent=80, min_days_to_keep=0)
    kept = qm._archive_gate(qm._scan_raw_iq(set()), usage_percent=90)
    assert kept == []
    _archive(drive)
    kept = qm._archive_gate(qm._scan_raw_iq(set()), usage_percent=90)
    assert sorted(e.path.parent.name for e in kept) == ["A", "B"]


def test_guardian_evict_date_keeps_unarchived_raw(site, monkeypatch):
    data, drive = site
    g = ResourceGuardian(n_channels=2, sample_rate=24000, data_root=data)
    monkeypatch.setattr(g, "_usage_percent", lambda: 90.0)
    g._evict_date(DAY)
    assert (data / "raw_buffer" / "A" / DAY).exists()
    _archive(drive)
    g._evict_date(DAY)
    assert not (data / "raw_buffer" / "A" / DAY).exists()


def test_guardian_evict_unit_keeps_unarchived_raw(site, monkeypatch):
    data, _ = site
    g = ResourceGuardian(n_channels=2, sample_rate=24000, data_root=data)
    monkeypatch.setattr(g, "_usage_percent", lambda: 90.0)
    assert g._evict_unit("raw_day", DAY) == 0
    assert (data / "raw_buffer" / "B" / DAY).exists()


def test_status_names_why_the_drive_is_full(tmp_path, monkeypatch):
    from tests.test_archiver_run import _env, _toml, _run, Usage, _day
    mount = _env(tmp_path, monkeypatch)
    wav = mount / "wav-archive"
    _day(wav, "20250101", 900)
    monkeypatch.setattr(A, "_usage", Usage(mount, 1000))
    real = A.shutil.rmtree

    def rm(p, *a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(A.shutil, "rmtree", rm)
    st = _run(tmp_path, _toml(tmp_path, mount, hw=50, evict_first=[wav]))
    assert st["state"] == "full"
    assert "permission" in (st["last_error"] or "").lower()
    assert st["evict_refused"] == 1
