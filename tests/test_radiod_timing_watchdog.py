"""Tests for the radiod timing watchdog — verdict classification + debounce."""
import json

import pytest

from hf_timestd.core import radiod_timing_watchdog as wd


@pytest.fixture
def status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "_STATUS_DIR", tmp_path)
    monkeypatch.setattr(wd, "INCIDENTS_PATH", tmp_path / "radiod-timing-incidents.jsonl")
    monkeypatch.setattr(wd, "STATUS_PATH", tmp_path / "radiod-timing-watchdog.json")
    return tmp_path


def _ext(**kw):
    return wd._ExternalClocks(**kw)


def _fire(monkeypatch, ext, *, delta, radiod_utc, system_now=1_782_700_000.0):
    """Fire one mapping jump with controlled external clocks + system time."""
    monkeypatch.setattr(wd.time, "time", lambda: system_now)
    monkeypatch.setattr(wd, "_read_gpsd", lambda now: ext)
    monkeypatch.setattr(wd, "_read_chrony", lambda ec: None)
    w = wd.RadiodTimingWatchdog()
    w.on_mapping_jump(channel="WWV_10000", gps_time_ns=0, rtp_timesnap=123,
                      radiod_utc=radiod_utc, old_utc=radiod_utc - delta,
                      delta_sec=delta)
    return w


def test_small_jump_is_ignored(status_dir, monkeypatch):
    """Sub-threshold jumps (jitter / ordinary re-anchor) are not incidents."""
    _fire(monkeypatch, _ext(gpsd_unix=1_782_700_000.0, gpsd_epoch_off_sec=0.0),
          delta=0.5, radiod_utc=1_782_700_000.0)
    assert not (status_dir / "radiod-timing-watchdog.json").exists()


def test_gps_source_bad_epoch(status_dir, monkeypatch):
    """gpsd reporting a 2016-era epoch → GPS receiver fault, not ka9q-radio."""
    now = 1_782_700_000.0  # 2026
    gpsd_2016 = 1_466_731_853.0
    _fire(monkeypatch,
          _ext(gpsd_unix=gpsd_2016, gpsd_epoch_off_sec=gpsd_2016 - now, gpsd_mode=3),
          delta=-489.0, radiod_utc=gpsd_2016, system_now=now)
    st = json.loads((status_dir / "radiod-timing-watchdog.json").read_text())
    assert st["verdict"] == "GPS_SOURCE_BAD_EPOCH"
    assert st["severity"] == "fail"
    assert "GPS RECEIVER fault" in st["detail"]


def test_radiod_bad_epoch_good_source(status_dir, monkeypatch):
    """radiod far off while gpsd is sane → candidate ka9q-radio issue (Phil)."""
    now = 1_782_700_000.0
    _fire(monkeypatch,
          _ext(gpsd_unix=now + 0.2, gpsd_epoch_off_sec=0.2, gpsd_mode=3),
          delta=600.0, radiod_utc=1_466_731_853.0, system_now=now)
    st = json.loads((status_dir / "radiod-timing-watchdog.json").read_text())
    assert st["verdict"] == "RADIOD_BAD_EPOCH_GOOD_SOURCE"
    assert st["severity"] == "fail"
    assert "ka9q-radio" in st["detail"] and "Phil" in st["detail"]


def test_sane_epochs_big_jump_is_warn(status_dir, monkeypatch):
    """A multi-second jump with sane epochs → warn (tear / slip), not fail."""
    now = 1_782_700_000.0
    _fire(monkeypatch, _ext(gpsd_unix=now, gpsd_epoch_off_sec=0.0, gpsd_mode=3),
          delta=4.0, radiod_utc=now, system_now=now)
    st = json.loads((status_dir / "radiod-timing-watchdog.json").read_text())
    assert st["verdict"] == "MAPPING_JUMP"
    assert st["severity"] == "warn"


def test_debounce_one_incident_per_cooldown(status_dir, monkeypatch):
    """A thrash trips many times/min; we capture once and count the rest."""
    now = 1_782_700_000.0
    monkeypatch.setattr(wd.time, "time", lambda: now)
    monkeypatch.setattr(wd, "_read_gpsd",
                        lambda n: _ext(gpsd_unix=now, gpsd_epoch_off_sec=0.0))
    monkeypatch.setattr(wd, "_read_chrony", lambda ec: None)
    # monotonic frozen so all fire within one cooldown window
    monkeypatch.setattr(wd.time, "monotonic", lambda: 1000.0)
    w = wd.RadiodTimingWatchdog()
    for _ in range(5):
        w.on_mapping_jump(channel="c", gps_time_ns=0, rtp_timesnap=1,
                          radiod_utc=now, old_utc=now - 10, delta_sec=-10.0)
    lines = (status_dir / "radiod-timing-incidents.jsonl").read_text().splitlines()
    assert len(lines) == 1  # only the first captured
    assert json.loads(lines[0])["suppressed_repeats"] == 0


def test_capture_never_raises(monkeypatch):
    """A failing evidence read must not propagate onto the recording path."""
    def boom(_now):
        raise RuntimeError("gpsd exploded")
    monkeypatch.setattr(wd, "_read_gpsd", boom)
    w = wd.RadiodTimingWatchdog()
    # Should swallow the exception, not raise.
    w.on_mapping_jump(channel="c", gps_time_ns=0, rtp_timesnap=1,
                      radiod_utc=1.0, old_utc=0.0, delta_sec=99.0)
