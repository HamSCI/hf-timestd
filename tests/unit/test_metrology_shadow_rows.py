"""Task 13: metrology writes one shadow row per broadcast it measured each
minute, and its channel's state transitions, to the durable T3 shadow
record (stream ``broadcasts-<channel>``)."""

import json
import logging

import pytest

from hf_timestd.core.metrology_service import MetrologyService
from tests.unit.synth_ticks import label_timing
from tests.unit.test_metrology_service_registration import (
    MIN,
    SR,
    T0,
    _Engine,
    _OWN_MAP,
    _meta,
    _minute,
    _service,
    _ticks,
)

DETAILS = {
    "WWV": {
        "delay_s": 0.0125,
        "sigma_ms": 0.8,
        "mode": "1F",
        "iono_source": "iri",
        "iono_giro": True,
        "iono_source_raw": "iri+giro",
    }
}


class _DetailEngine(_Engine):
    """The stand-in, answering with mode and ionospheric source."""

    def expected_delay_details(self, system_time, utc_minute):
        return {s: dict(d) for s, d in DETAILS.items()}

    def expected_delays_s(self, system_time, utc_minute):
        return {s: d["delay_s"] for s, d in DETAILS.items()}


def _shadow_service(tmp_path, **shadow_cfg):
    svc = _service(tmp_path)
    svc.engine = _DetailEngine()
    svc.frequency_hz = 10e6
    svc._init_shadow({"timing": {"t3_shadow": {
        "directory": str(tmp_path / "shadow"), **shadow_cfg}}})
    return svc


def _rows(tmp_path):
    files = sorted((tmp_path / "shadow").glob("broadcasts-SHARED_10000-*.jsonl"))
    out = []
    for f in files:
        for line in f.read_text().splitlines():
            assert len(line.encode()) < 2048
            out.append(json.loads(line))
    return out


def test_each_measured_station_writes_one_row_per_minute(tmp_path):
    svc = _shadow_service(tmp_path)
    measured = {}
    for k in range(5):
        _minute(svc, k, audio=_ticks(k, marker=False))
        if "WWV" in svc._last_arrivals:
            measured[MIN + 60 * k] = (svc._last_arrivals["WWV"], 1_000_000 + k * 60 * SR)
    assert len(measured) >= 3

    rows = [r for r in _rows(tmp_path) if r["kind"] == "broadcast"]
    assert [r["utc_minute"] for r in rows] == sorted(measured)
    for r in rows:
        a, start_rtp = measured[r["utc_minute"]]
        assert r["channel"] == "SHARED_10000"
        assert r["frequency_hz"] == 10e6
        assert r["station"] == "WWV"
        assert r["expected_delay_ms"] == pytest.approx(12.5)
        assert r["model_sigma_ms"] == pytest.approx(0.8)
        assert r["mode"] == "1F"
        assert r["iono_source"] == "iri"
        assert r["iono_giro"] is True
        assert r["iono_source_raw"] == "iri+giro"
        assert r["residual_ms"] == pytest.approx(a.residual_ms, abs=1e-3)
        assert r["sigma_ms"] == pytest.approx(a.sigma_ms, abs=1e-3)
        assert r["fold_snr_db"] == pytest.approx(a.fold_snr_db, abs=0.01)
        assert r["arrival_rtp"] == pytest.approx(a.arrival_rtp, abs=1e-3)
        assert r["arrival_ms_in_plane"] == pytest.approx(a.residual_ms + 12.5, abs=1e-3)
        # The map is linear in RTP: count from this minute's start sample.
        expect = (_OWN_MAP.input_index(start_rtp)
                  + (a.arrival_rtp - start_rtp) * _OWN_MAP.ilen / _OWN_MAP.olen)
        assert r["arrival_input_index"] == pytest.approx(expect, abs=1.0)
        assert r["rtp_domain"] == "SHARED_10000"
        assert r["t3_state"] in ("CANDIDATE", "ACQUIRED")
        assert r["predictions"] == [{
            "model": "hop", "delay_ms": pytest.approx(12.5),
            "sigma_ms": pytest.approx(0.8), "mode": "1F",
            "iono_source": "iri", "iono_giro": True}]
    assert rows[-1]["t3_state"] == "ACQUIRED"


def test_no_counter_map_writes_a_null_input_index(tmp_path):
    svc = _shadow_service(tmp_path)
    for k in range(3):
        meta = _meta(k, 0.250)
        del meta["counter_map"]
        svc.apply_registration(label_timing(T0 + 60 * k, 0.250, SR),
                               _ticks(k, marker=False), 1_000_000 + k * 60 * SR,
                               MIN + 60 * k, meta)
    rows = [r for r in _rows(tmp_path) if r["kind"] == "broadcast"]
    assert rows
    assert all(r["arrival_input_index"] is None for r in rows)


def test_channel_state_changes_are_rows_and_warnings(tmp_path, caplog):
    svc = _shadow_service(tmp_path)
    caplog.set_level(logging.WARNING)
    # BOOTSTRAP: an old producer's minute, no raw radiod pair.
    meta = _meta(0, 0.0)
    del meta["raw_gps_time_ns"], meta["raw_rtp_timesnap"]
    label = label_timing(T0, 0.0, SR)
    label.judge_tier = "T6"
    svc.apply_registration(label, _ticks(0), 1_000_000, MIN, meta)
    caplog.clear()
    # CANDIDATE, then WITNESS (T6 station: the plane verifies next minute).
    for k in (1, 2):
        _minute(svc, k, walk=0.0, t6=True)
    rows = [r for r in _rows(tmp_path) if r["kind"] == "transition"]
    assert [(r["from"], r["to"]) for r in rows] == [
        (None, "BOOTSTRAP"), ("BOOTSTRAP", "CANDIDATE"), ("CANDIDATE", "WITNESS")]
    assert rows[0]["reason"] == "startup"
    assert [r["utc_minute"] for r in rows] == [MIN, MIN + 60, MIN + 120]
    assert all(r["scope"] == "SHARED_10000" for r in rows)
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "T3 STATE" in r.getMessage()]
    assert len(warns) == 2
    assert svc._shadow_transitions.count == 2
    assert rows[-1]["count"] == 2


def test_shadow_disabled_writes_nothing(tmp_path):
    svc = _shadow_service(tmp_path, enabled=False)
    for k in range(3):
        _minute(svc, k, audio=_ticks(k, marker=False))
    assert svc._last_arrivals
    assert not (tmp_path / "shadow").exists()


def test_config_defaults_and_overrides(tmp_path):
    svc = MetrologyService.__new__(MetrologyService)
    svc.channel_name = "WWV_10000"
    svc._init_shadow({})
    assert svc._shadow_record.stream == "broadcasts-WWV_10000"
    assert str(svc._shadow_record.directory) == "/var/lib/timestd/t3_shadow"
    assert svc._shadow_record.retention_days == 14
    svc._init_shadow({"timing": {"t3_shadow": {
        "directory": str(tmp_path), "retention_days": 3}}})
    assert svc._shadow_record.directory == tmp_path
    assert svc._shadow_record.retention_days == 3
