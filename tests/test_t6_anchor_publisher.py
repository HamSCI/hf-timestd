"""t6-anchor.json: the live T6 tie hf-timestd publishes for its clients.

The round-trip test reads the file through hamsci-dsp's own reader, so the
writer produces exactly what the reader accepts.
"""
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "unit"))   # synth_origins

from synth_origins import ILEN, counter_map_for, olen_for, rtp_at  # noqa: E402

from hf_timestd.core.native_anchor import NativeAnchor  # noqa: E402
from hf_timestd.core.t6_anchor_publisher import (  # noqa: E402
    T6AnchorPublisher, build_t6_anchor, write_t6_anchor,
)

F_IN = 64_800_000
U0_NS = 1_800_000_000 * 10**9          # UTC of input sample 0
T6_K0 = 40
O6 = olen_for(96_000)


@pytest.fixture
def fake_map():
    return counter_map_for(T6_K0, 96_000)


@pytest.fixture
def fake_anchor():
    """A 96 kHz anchor 4 ms (384 samples) into input block 102."""
    block = 102
    rtp = (rtp_at(block, T6_K0, O6) + 384) % 2**32
    idx = block * ILEN + 384 * ILEN // O6
    utc = U0_NS + idx * 10**9 // F_IN
    return NativeAnchor(anchor_rtp=rtp, anchor_utc_ns=utc, sample_rate_hz=96_000,
                        chain_delay_ns=0, captured_at_utc_ns=utc,
                        captured_via_tier="T5")


def _kw(fake_anchor, fake_map, **over):
    kw = dict(anchor=fake_anchor, t6_map=fake_map, authoritative=True,
              authority_state="AUTHORITATIVE", sigma_ns=150.0,
              counter_epoch_id="pair-1", radiod_status="b4",
              now_utc_iso="2026-11-19T12:00:00Z")
    kw.update(over)
    return kw


def test_null_index_unless_authoritative(fake_anchor, fake_map):
    d = build_t6_anchor(**_kw(fake_anchor, fake_map, authoritative=False,
                              authority_state="DEGRADED",
                              radiod_status="b4-status.local"))
    assert d["anchor_input_index"] is None and d["t6_authority_state"] == "DEGRADED"


def test_measured_sigma_else_assumed(fake_anchor, fake_map):
    m = build_t6_anchor(**_kw(fake_anchor, fake_map, sigma_ns=150.0))
    assert m["a_level_provenance"] == "measured" and m["u_epoch_ns"] == 150.0
    a = build_t6_anchor(**_kw(fake_anchor, fake_map, sigma_ns=None))
    assert (a["u_epoch_ns"], a["a_level_provenance"]) == (230.0, "assumed")
    assert a["anchor_utc_ns"] == fake_anchor.anchor_utc_ns
    assert isinstance(a["anchor_utc_ns"], int)
    assert (a["k"], a["p"]) == (2.0, None)


def test_non_finite_sigma_is_assumed(fake_anchor, fake_map):
    a = build_t6_anchor(**_kw(fake_anchor, fake_map, sigma_ns=float("nan")))
    assert (a["u_epoch_ns"], a["a_level_provenance"]) == (230.0, "assumed")


def test_status_and_address_kept(fake_anchor, fake_map):
    d = build_t6_anchor(**_kw(fake_anchor, fake_map,
                              radiod_status="b4-status.local",
                              radiod_address="10.0.0.5"))
    assert d["radiod_status"] == "b4-status.local"
    assert d["radiod_address"] == "10.0.0.5"


def test_written_world_readable(tmp_path):
    p = tmp_path / "t6-anchor.json"
    write_t6_anchor({"schema": 1}, p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644
    assert json.loads(p.read_text()) == {"schema": 1}
    assert [x.name for x in tmp_path.iterdir()] == ["t6-anchor.json"]


STAMP_S = int(datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp())


def test_round_trip_through_the_hamsci_dsp_reader(tmp_path, fake_anchor, fake_map):
    from hamsci_dsp.t6_anchor import read_t6_anchor
    d = build_t6_anchor(**_kw(fake_anchor, fake_map))
    p = tmp_path / "t6-anchor.json"
    write_t6_anchor(d, p)
    a = read_t6_anchor(p, now_ns=(STAMP_S + 30) * 10**9)   # 30 s after the stamp
    assert a is not None
    assert a.utc_ns_at(a.anchor_input_index) == fake_anchor.anchor_utc_ns
    assert (a.radiod_status, a.counter_epoch_id) == ("b4", "pair-1")
    assert (a.input_samprate, a.u_epoch_ns, a.k, a.p) == (F_IN, 150.0, 2.0, None)
    assert a.input_epoch_gps_s == fake_map.input_epoch_gps_s
    assert a.a_level_provenance == "measured"


def test_non_authoritative_reads_as_none(tmp_path, fake_anchor, fake_map):
    from hamsci_dsp.t6_anchor import read_t6_anchor
    d = build_t6_anchor(**_kw(fake_anchor, fake_map, authoritative=False,
                              authority_state="DEGRADED"))
    p = tmp_path / "t6-anchor.json"
    write_t6_anchor(d, p)
    assert read_t6_anchor(p, now_ns=(STAMP_S + 1) * 10**9) is None


def test_publisher_writes_once_a_minute(tmp_path, fake_anchor, fake_map):
    now = [1000.0]
    p = tmp_path / "t6-anchor.json"
    pub = T6AnchorPublisher(path=p, mono_fn=lambda: now[0])
    kw = _kw(fake_anchor, fake_map)
    kw.pop("now_utc_iso")
    assert pub.maybe_publish(**kw) is True
    now[0] += 30
    assert pub.maybe_publish(**kw) is False
    now[0] += 31
    assert pub.maybe_publish(**kw) is True


def test_publisher_write_failure_returns_false(tmp_path, fake_anchor, fake_map):
    pub = T6AnchorPublisher(path=tmp_path / "no-such-dir" / "t6.json",
                            mono_fn=lambda: 1.0)
    kw = _kw(fake_anchor, fake_map)
    kw.pop("now_utc_iso")
    assert pub.maybe_publish(**kw) is False
