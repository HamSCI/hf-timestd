"""Tests for ChainDelayStore, the on-disk record of a station's T6 chain delay."""
import json
import logging
import os

import pytest

from hf_timestd.core.chain_delay_store import ChainDelayRecord, ChainDelayStore

IDENT = {
    "t6_frequency_hz": 45375000.0,
    "t6_sample_rate": 96000.0,
    "low_edge_hz": -25000.0,
    "high_edge_hz": 25000.0,
}


def make_record(offset_ns: float = -16.35e6, state: str = "confirmed") -> ChainDelayRecord:
    return ChainDelayRecord(
        offset_ns=offset_ns, sigma_ns=140e3, spread_ns=1.11e6,
        n=720, n_eff=120.0, span_s=7200.0,
        learned_from_utc="2026-10-27T00:00:00Z",
        learned_to_utc="2026-10-27T02:00:00Z",
        lower_tier="T4", state=state,
    )


def read_file(path):
    return json.loads(path.read_text())


def test_round_trip(tmp_path):
    path = tmp_path / "t6_chain_delay.json"
    store = ChainDelayStore(path, IDENT)
    rec = make_record(state="seeded")
    assert store.set_record(rec) is True
    other = ChainDelayStore(path, IDENT)
    other.load()
    assert other.record == rec
    assert other.record.state == "seeded"
    assert read_file(path)["schema"] == ChainDelayStore.SCHEMA


def test_candidate_round_trip_and_promote(tmp_path):
    path = tmp_path / "c.json"
    store = ChainDelayStore(path, IDENT)
    store.set_record(make_record(-16e6))
    store.set_candidate(make_record(-17e6))
    other = ChainDelayStore(path, IDENT)
    other.load()
    assert other.candidate.offset_ns == -17e6
    assert other.promote_candidate() is True
    assert other.record.offset_ns == -17e6
    assert other.candidate is None
    assert read_file(path)["history"][-1]["offset_ns"] == -16e6


def test_discard_pushes_record_then_candidate(tmp_path):
    store = ChainDelayStore(tmp_path / "c.json", IDENT)
    store.set_record(make_record(-16e6))
    store.set_candidate(make_record(-17e6))
    assert store.discard() is True
    assert store.record is None and store.candidate is None
    hist = read_file(tmp_path / "c.json")["history"]
    assert [h["offset_ns"] for h in hist] == [-16e6, -17e6]
    assert all("replaced_utc" in h for h in hist)


def test_identity_change_sets_the_record_aside(tmp_path, caplog):
    path = tmp_path / "t6_chain_delay.json"
    ChainDelayStore(path, IDENT).set_record(make_record())
    new_ident = dict(IDENT, t6_frequency_hz=45400000.0)
    store = ChainDelayStore(path, new_ident)
    with caplog.at_level(logging.WARNING):
        store.load()
    assert store.record is None
    data = read_file(path)
    assert data["history"][-1]["offset_ns"] == -16.35e6
    assert data["identity"]["t6_frequency_hz"] == 45400000.0
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_history_keeps_ten(tmp_path):
    path = tmp_path / "c.json"
    store = ChainDelayStore(path, IDENT)
    for i in range(12):
        store.set_record(make_record(float(i)))
    hist = read_file(path)["history"]
    assert len(hist) == 10
    assert hist[-1]["offset_ns"] == 10.0  # newest last


def test_corrupt_file_is_moved_aside(tmp_path, caplog):
    path = tmp_path / "t6_chain_delay.json"
    path.write_text("{not json")
    store = ChainDelayStore(path, IDENT)
    with caplog.at_level(logging.ERROR):
        store.load()
    assert store.record is None
    assert list(tmp_path.glob("t6_chain_delay.json.corrupt-*"))
    assert not path.exists()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_missing_file_loads_empty(tmp_path):
    store = ChainDelayStore(tmp_path / "nope.json", IDENT)
    store.load()
    assert store.record is None and store.candidate is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unwritable_dir_keeps_the_record_in_memory(tmp_path, caplog):
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        store = ChainDelayStore(d / "c.json", IDENT)
        rec = make_record()
        with caplog.at_level(logging.ERROR):
            assert store.set_record(rec) is False
            assert store.record == rec
            errors = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert len(errors) == 1
            assert store.set_record(make_record(-1.0)) is False
            errors = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert len(errors) == 1
        d.chmod(0o700)
        assert store.set_record(make_record(-2.0)) is True
        d.chmod(0o500)
        with caplog.at_level(logging.ERROR):
            store.set_record(make_record(-3.0))
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2
    finally:
        d.chmod(0o700)


def test_atomic_write_leaves_no_temp_file(tmp_path):
    store = ChainDelayStore(tmp_path / "t6_chain_delay.json", IDENT)
    store.set_record(make_record())
    store.set_candidate(make_record(-1.0))
    assert [p.name for p in tmp_path.iterdir()] == ["t6_chain_delay.json"]


def test_request_round_trip(tmp_path):
    store = ChainDelayStore(tmp_path / "t6_chain_delay.json", IDENT)
    assert store.request_path.name == "t6_chain_delay.json.request.json"
    ChainDelayStore.write_request(store.request_path, {"action": "accept"})
    assert store.take_request() == {"action": "accept"}
    assert not store.request_path.exists()
    assert store.take_request() is None
    store.request_path.write_text("{garbage")
    assert store.take_request() is None
    assert not store.request_path.exists()


def test_to_status(tmp_path):
    store = ChainDelayStore(tmp_path / "c.json", IDENT)
    assert store.to_status()["record"] is None
    store.set_record(make_record())
    st = store.to_status()
    assert st["record"]["offset_ns"] == -16.35e6
    assert st["candidate"] is None
    json.dumps(st)


def test_non_utf8_file_is_moved_aside(tmp_path, caplog):
    path = tmp_path / "t6_chain_delay.json"
    path.write_bytes(b"\xff\xfe\x00")
    store = ChainDelayStore(path, IDENT)
    with caplog.at_level(logging.ERROR):
        store.load()
    assert store.record is None
    assert list(tmp_path.glob("t6_chain_delay.json.corrupt-*"))
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_undeletable_request_is_not_returned(tmp_path, caplog):
    d = tmp_path / "rq"
    d.mkdir()
    store = ChainDelayStore(d / "c.json", IDENT)
    ChainDelayStore.write_request(store.request_path, {"action": "accept"})
    d.chmod(0o500)
    try:
        with caplog.at_level(logging.ERROR):
            assert store.take_request() is None
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    finally:
        d.chmod(0o700)


def test_non_utf8_request_is_dropped(tmp_path):
    store = ChainDelayStore(tmp_path / "c.json", IDENT)
    store.request_path.write_bytes(b"\xff\xfe")
    assert store.take_request() is None
    assert not store.request_path.exists()


@pytest.mark.parametrize("field, value", [
    ("offset_ns", "abc"),
    ("offset_ns", True),
    ("sigma_ns", 0.0),
    ("spread_ns", -1.0),
    ("n_eff", -1.0),
    ("span_s", None),
    ("state", "maybe"),
])
def test_a_field_of_the_wrong_type_or_range_is_corrupt(tmp_path, caplog,
                                                       field, value):
    path = tmp_path / "t6_chain_delay.json"
    ChainDelayStore(path, IDENT).set_record(make_record())
    data = read_file(path)
    data[field] = value
    path.write_text(json.dumps(data))
    store = ChainDelayStore(path, IDENT)
    with caplog.at_level(logging.ERROR):
        store.load()
    assert store.record is None
    assert list(tmp_path.glob("t6_chain_delay.json.corrupt-*"))
    assert not path.exists()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_a_nan_offset_is_corrupt(tmp_path):
    path = tmp_path / "t6_chain_delay.json"
    ChainDelayStore(path, IDENT).set_record(make_record())
    path.write_text(path.read_text().replace("-16350000.0", "NaN"))
    store = ChainDelayStore(path, IDENT)
    store.load()
    assert store.record is None
    assert list(tmp_path.glob("t6_chain_delay.json.corrupt-*"))
