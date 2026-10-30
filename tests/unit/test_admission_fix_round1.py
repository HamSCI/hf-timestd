"""Fix round 1 on rulings B and C: the review's minors m-1, m-2, m-3,
m-6 and m-7 (admission-review.md, 2026-10-30)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns
from hf_timestd.core.offset_judge import BenchReading, OffsetJudge

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)


def _cfg_file(tmp_path, admitted: bool) -> Path:
    p = tmp_path / "timestd-config.toml"
    p.write_text(f"[timing.t3]\nadmitted = {'true' if admitted else 'false'}\n",
                 encoding="utf-8")
    return p


# ── m-1: an unadmitted T3 never witnesses either ─────────────────────


def test_closing_t3_clears_witness_only(tmp_path):
    from hf_timestd.core.authority_manager import AuthorityManager, ProbeResult

    class _P:
        t_level = "T3"

        def poll(self):
            return ProbeResult("T3", False, offset_ms=40.0, sigma_ms=1.0,
                               frame="rtp", witness_only=True, reason="w")

    m = AuthorityManager(probes=[_P()], output_path=tmp_path / "a.json",
                         a_level_provider=lambda: "A1")
    results = {"T3": _P().poll()}
    m._close_unadmitted_t3(results)
    assert results["T3"].available is False
    assert results["T3"].witness_only is False
    assert results["T3"].detail["probe_witness_only"] is True


# ── m-6: the key in each process's own config reaches the switch ─────


def test_the_recorder_reads_the_switch_from_its_config(tmp_path, monkeypatch):
    import ka9q.utils

    import hf_timestd.core.core_recorder_v2 as crv
    import hf_timestd.core.offset_judge as oj

    seen = {}

    class _Judge:
        def __init__(self, *a, **k):
            seen.update(k)

        def start(self):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    class _Dummy:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(oj, "OffsetJudge", _Judge)
    monkeypatch.setattr(ka9q.utils, "resolve_multicast_address",
                        lambda *a, **k: "239.1.1.1")
    monkeypatch.setattr(crv, "RadiodControl", _Dummy)
    monkeypatch.setattr(crv, "CounterMapProbe", _Dummy)
    for admitted in (True, False):
        seen.clear()
        rec = crv.CoreRecorderV2({
            "output_dir": str(tmp_path / f"rec-{admitted}"),
            "ka9q": {"status": "hf-status.local"},
            "timing": {"t3": {"admitted": admitted}}})
        assert rec._t3_admitted is admitted
        assert seen["t3_admitted"] is admitted


def test_metrology_reads_the_switch_from_its_config(tmp_path, monkeypatch):
    import toml

    import hf_timestd.core.metrology_service as ms

    class _Dummy:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(ms, "make_data_product_writer", lambda *a, **k: _Dummy())
    for admitted in (True, False):
        # The dict main() builds from the TOML it loads.
        config = ms.metrology_config_from_toml(
            toml.load(str(_cfg_file(tmp_path, admitted))))
        config["timing"]["t3_shadow"] = {"enabled": False}
        config["metrology"] = {"realtime_iono": False, "physics_products": False}
        svc = ms.MetrologyService(config, "WWV_10000", 10e6,
                                  tmp_path / f"met-{admitted}", "EM38ww")
        assert svc._t3_admitted is admitted


def test_fusion_reads_the_switch_from_its_own_config_path(tmp_path, monkeypatch):
    """m-2: fusion used to re-read a hard-coded /etc path.  It now reads the
    file the unit names, and still fails closed on a missing one."""
    import hf_timestd.core.multi_broadcast_fusion as mbf
    from hf_timestd.core.authority_runner import build_authority_runner_from_config

    for admitted in (True, False):
        cfg = mbf.load_fusion_config(_cfg_file(tmp_path, admitted))
        runner = build_authority_runner_from_config(cfg)
        assert runner.manager.t3_admitted is admitted
    assert mbf.load_fusion_config(tmp_path / "missing.toml") == {}

    class _Stop(BaseException):
        pass

    paths = []

    def _load(path):
        paths.append(Path(path))
        raise _Stop

    monkeypatch.setattr(mbf, "load_fusion_config", _load)
    p = _cfg_file(tmp_path, True)
    with pytest.raises(_Stop):
        mbf.run_fusion_service(tmp_path / "data", config_path=p)
    assert paths == [p]


# ── m-7: the witness block, closure off (B4's regime) ────────────────


class _Bench:
    def __init__(self, tier, utc_off, plane, bench, authoritative=None):
        self.tier, self.utc_off, self.plane, self.bench = tier, utc_off, plane, bench
        self.authoritative = authoritative

    def poll(self):
        d = {"bench": self.bench}
        if self.authoritative is not None:
            d["authoritative"] = self.authoritative
        return BenchReading(tier=self.tier, utc=WALL0 + self.utc_off,
                            sigma_ns=1e6, mono=1000.0, detail=d, plane=self.plane)


@pytest.mark.parametrize("admitted", [False, True])
def test_the_witness_block_holds_no_unadmitted_t3_with_the_closure_off(
        tmp_path, admitted):
    judge = OffsetJudge(
        config={"enabled": True,
                "chain_delay_path": str(tmp_path / "cd.json")},
        benches=[_Bench("T4", 0.0, "host", "chrony_lan"),
                 _Bench("T3", 0.010, "label", "hf_acquired", authoritative=True)],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: WALL0, mono_fn=lambda: 1000.0,
        t3_admitted=admitted)
    judge.register_radiod_pair(
        KEY, unix_ns_to_gps_time_ns(int(round(WALL0 * 1e9))), 0, SR)
    judge.tick()
    published = json.loads((tmp_path / "offset_judge.json").read_text())
    assert published["label_plane_anchor"] is None          # closure off
    witness = published["label_plane_witness"]
    if admitted:
        assert witness is not None and witness["bench"] == "hf_acquired"
    else:
        assert witness is None


# ── m-3: absent minutes do not count as kept ─────────────────────────


class _Plane:
    def __init__(self, channel, verified=True):
        self.channel, self.verified, self.method = channel, verified, "acquired"

    def in_domain(self, *a):
        return self

    def sample0_utc_for(self, rtp):
        return WALL0


def test_absent_minutes_do_not_end_a_drop_episode(tmp_path, caplog):
    """m-3: a minute with no fused plane, or no verified own plane (say
    BOOTSTRAP after a restart), kept nothing.  Ten of them closed the
    episode and logged "keeps this channel's own plane again"."""
    from tests.unit.test_metrology_service_counter_domains import _channel

    svc = _channel(tmp_path, "SHARED_10000")
    own = _Plane("SHARED_10000")
    sibs = [_Plane("WWV_15000"), _Plane("WWV_25000")]
    fused = _Plane("fused")
    with caplog.at_level(logging.INFO, logger="hf_timestd.core.metrology_service"):
        svc._note_fusion_drops(own, [own] + sibs, fused, ["WWV_15000", "WWV_25000"],
                               [], 0, None)
        for _ in range(svc._OWN_DROP_CLEAR_MIN + 2):
            svc._note_fusion_drops(None, sibs, None, [], [], 0, None)
            svc._note_fusion_drops(_Plane("SHARED_10000", verified=False),
                                   [own] + sibs, fused, ["SHARED_10000"], [], 0, None)
        svc._note_fusion_drops(own, [own] + sibs, fused, ["WWV_15000", "WWV_25000"],
                               [], 0, None)
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert not [m for lv, m in msgs if "keeps this channel" in m], msgs
    assert len([m for lv, m in msgs if lv == logging.WARNING and "dropped" in m]) == 1
