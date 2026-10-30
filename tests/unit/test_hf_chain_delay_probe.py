"""Stage-0 probe: clones the HF channels onto the TS-1 pilot, against fakes."""
from types import SimpleNamespace

import numpy as np
import pytest

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.hf_chain_delay_probe import HfChainDelayProbe, ProbeChannelSpec

from synth_origins import EPOCH_GPS_S, F_IN, ILEN, olen_for, rtp_at

PILOT = 19_425_000
HF = [("SHARED_2500", 2_500_000), ("SHARED_5000", 5_000_000),
      ("SHARED_10000", 10_000_000), ("SHARED_15000", 15_000_000),
      ("WWV_20000", 20_000_000), ("WWV_25000", 25_000_000)]


def make_config(enabled=True, pilot=PILOT):
    return {
        "timing": {"t6_pps": {"enabled": enabled, "frequency_hz": pilot,
                              "sample_rate": 96_000, "low_edge_hz": -25_000,
                              "high_edge_hz": 25_000}},
        "recorder": {"channel_group": {"timestd": {
            "preset": "iq", "sample_rate": 24_000, "low_edge_hz": -100,
            "high_edge_hz": 100,
            "channels": [{"frequency_hz": f, "description": d} for d, f in HF]}}},
    }


def reply_for(spec, k0, block):
    olen = olen_for(spec.sample_rate)
    return SimpleNamespace(
        gps_time=int(round((EPOCH_GPS_S + block * ILEN / F_IN) * 1e9)),
        rtp_timesnap=rtp_at(block, k0, olen),
        frontend=SimpleNamespace(input_samples=block * ILEN, input_samprate=F_IN),
        filter_blocksize=ILEN, filter_fir_length=324_001,
        filter2=SimpleNamespace(fir_length=None),
        output_samprate=spec.sample_rate, low_edge=spec.low_edge_hz,
        high_edge=spec.high_edge_hz, output_encoding=4,
        output_data_dest_socket={"family": "IPv4", "address": "239.1.2.3", "port": 5004})


class FakeControl:
    """Records create/remove; answers poll_status from a per-ssrc script."""

    def __init__(self, specs, k0s=None, reply_fn=None):
        self.by_ssrc = {s.ssrc: s for s in specs}
        self.k0s = k0s or {s.ssrc: 51 for s in specs}
        self.reply_fn = reply_fn
        self.created, self.removed, self.polls = [], [], {}
        self.live = set()           # SSRCs that already exist on this radiod

    def create_channel(self, **kw):
        self.created.append(kw)
        return kw["ssrc"]

    def remove_channel(self, ssrc):
        self.removed.append(ssrc)

    def poll_status(self, ssrc, timeout=2.0):
        if ssrc in self.live:
            return SimpleNamespace(output_samprate=24_000, frequency=7_000_000.0)
        if ssrc not in {k["ssrc"] for k in self.created}:      # ghost: frontend only
            return SimpleNamespace(frontend=SimpleNamespace(input_samples=1))
        n = self.polls.get(ssrc, 0)
        self.polls[ssrc] = n + 1
        if self.reply_fn:
            return self.reply_fn(self, self.by_ssrc[ssrc], n)
        return reply_for(self.by_ssrc[ssrc], self.k0s[ssrc], 100 + n)


class FakeStream:
    def __init__(self, on_samples, fail=False):
        self.on_samples, self.fail, self.started, self.stopped = on_samples, fail, False, False

    def start(self):
        if self.fail:
            raise RuntimeError("socket gone")
        self.started = True
        for i in range(3):
            self.on_samples(np.zeros(8, dtype=np.complex64),
                            SimpleNamespace(delivered_rtp_start=1000 + i))

    def stop(self):
        self.stopped = True


class FakeFine:
    def __init__(self, sample_rate, fold_seconds=30):
        self.sample_rate, self.fold_seconds, self.n = sample_rate, fold_seconds, 0

    def process_samples(self, iq, rtp):
        self.n += 1
        return FineEdgeEstimate(edge_offset_samples=0.0, edge_rtp=rtp,
                                edge_subsample=0.25, n_seconds_folded=30,
                                plateau_amplitude=1.0, fit_rms=0.01)


class Clock:
    def __init__(self):
        self.t = 0.0

    def mono(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def make_probe(control, streams=None, fail_stream=False, grants=None):
    clock = Clock()
    streams = streams if streams is not None else []

    def stream_factory(spec, on_samples, grant):
        if grants is not None:
            grants.append((spec, grant))
        s = FakeStream(on_samples, fail=fail_stream)
        streams.append(s)
        return s

    return HfChainDelayProbe(control, stream_factory, fine_stage_factory=FakeFine,
                             mono_fn=clock.mono, sleep_fn=clock.sleep)


def specs():
    return HfChainDelayProbe.plan_channels(make_config())


def test_plan_clones_t6_and_every_hf_channel_on_the_pilot():
    sp = specs()
    assert [s.label for s in sp] == ["T6_REF"] + [d for d, _ in HF]
    assert {s.frequency_hz for s in sp} == {float(PILOT)}
    assert len({s.ssrc for s in sp}) == 7
    assert sp[0].ssrc == HfChainDelayProbe.SSRC_BASE
    assert (sp[0].sample_rate, sp[0].low_edge_hz, sp[0].high_edge_hz) == (96_000, -25_000, 25_000)
    assert sp[1].sample_rate == 24_000 and sp[1].preset == "iq"
    assert (sp[1].low_edge_hz, sp[1].high_edge_hz) == (-100, 100)


@pytest.mark.parametrize("enabled,pilot", [(False, PILOT), (True, 0)])
def test_plan_refuses_without_a_pilot(enabled, pilot):
    with pytest.raises(ValueError, match="no TS-1 pilot"):
        HfChainDelayProbe.plan_channels(make_config(enabled, pilot))


def test_plan_lets_a_channel_override_the_group_edges():
    cfg = make_config()
    cfg["recorder"]["channel_group"]["timestd"]["channels"][0]["low_edge_hz"] = -300
    assert specs_from(cfg)[1].low_edge_hz == -300


def specs_from(cfg):
    return HfChainDelayProbe.plan_channels(cfg)


def test_clones_are_created_with_explicit_ssrcs_and_a_lifetime():
    sp = specs()
    control = FakeControl(sp)
    make_probe(control).run(sp, 5)
    assert sorted(k["ssrc"] for k in control.created) == sorted(s.ssrc for s in sp)
    assert all(k["lifetime"] == HfChainDelayProbe.LIFETIME_S for k in control.created)
    assert all(k["frequency_hz"] == float(PILOT) for k in control.created)


def test_every_clone_is_removed_even_when_streaming_fails():
    sp = specs()
    control = FakeControl(sp)
    with pytest.raises(RuntimeError, match="socket gone"):
        make_probe(control, fail_stream=True).run(sp, 5)
    assert sorted(control.removed) == sorted(s.ssrc for s in sp)


def test_every_clone_is_removed_after_a_normal_run_and_streams_stop():
    sp = specs()
    control, streams = FakeControl(sp), []
    make_probe(control, streams).run(sp, 5)
    assert sorted(control.removed) == sorted(s.ssrc for s in sp)
    assert len(streams) == 7 and all(s.stopped for s in streams)


def _damaged_first(damage, n_bad):
    def reply(control, spec, n):
        r = reply_for(spec, 51, 100 + n)
        if n < n_bad:
            damage(r)
        return r
    return reply


def _ghost(r):
    r.rtp_timesnap = None                      # frontend status only


DAMAGES = [
    _ghost,
    lambda r: setattr(r, "gps_time", 0),
    lambda r: setattr(r, "gps_time", None),
    lambda r: setattr(r.frontend, "input_samples", None),
    lambda r: setattr(r, "output_samprate", 48_000),
]


@pytest.mark.parametrize("damage", DAMAGES)
def test_bad_replies_are_not_polls_and_never_enter_a_map(damage):
    # Three bad replies carry a wrong origin; had they counted they would sit in the window.
    sp = specs()

    def reply(control, spec, n):
        r = reply_for(spec, 58 if n < 3 else 51, 100 + n)
        if n < 3:
            damage(r)
        return r

    caps, epochs = make_probe(FakeControl(sp, reply_fn=reply)).run(sp, 6)
    assert all(c.counter_map is not None and c.counter_map.n_agree == 5
               for c in caps)
    assert len(epochs) == 7       # one grant poll after the 3 bad ones, then 6 loop polls


@pytest.mark.parametrize("damage", DAMAGES)
def test_a_clone_that_only_ever_answers_badly_aborts_and_is_removed(damage):
    sp = specs()
    control = FakeControl(sp, reply_fn=_damaged_first(damage, 10**6))
    with pytest.raises(RuntimeError, match="never answered"):
        make_probe(control).run(sp, 5)
    assert sorted(control.removed) == sorted(s.ssrc for s in sp)


def test_a_poll_that_raises_is_skipped_not_fatal():
    sp = specs()

    def flaky(control, spec, n):
        if n == 0:
            raise TimeoutError("no answer")
        return reply_for(spec, 51, 100 + n)

    caps, _ = make_probe(FakeControl(sp, reply_fn=flaky)).run(sp, 6)
    assert all(c.counter_map is not None for c in caps)


def test_run_returns_edges_maps_and_pair_epochs():
    sp = specs()
    k0s = {s.ssrc: (40 if i == 0 else 51) for i, s in enumerate(sp)}
    caps, epochs = make_probe(FakeControl(sp, k0s=k0s)).run(sp, 5)
    assert [c.label for c in caps] == [s.label for s in sp]
    assert caps[0].edges and caps[0].counter_map is not None and len(epochs) >= 5
    assert all(e == pytest.approx(EPOCH_GPS_S, abs=1e-6) for e in epochs)
    assert caps[0].edges[0].edge_rtp == 1000 and caps[0].edges[0].edge_subsample == 0.25
    assert caps[0].output_samprate == 96_000 and caps[1].output_samprate == 24_000
    assert caps[0].fir_length == 324_001 and caps[0].input_samprate == F_IN


def test_map_comes_from_the_most_recent_five_polls_not_all_of_them():
    # Five early polls come from another origin.  All ten together tie 5-5
    # and from_polls would pick the first group; the latest five agree 5-0.
    sp = specs()

    def early_other_origin(control, spec, n):
        return reply_for(spec, 51 if n >= 5 else 58, 100 + n)

    caps, _ = make_probe(FakeControl(sp, reply_fn=early_other_origin)).run(sp, 10)
    assert all(c.counter_map is not None for c in caps)
    assert all(c.counter_map.n_agree == 5 for c in caps)
    # A 5-5 tie would silently pick the early (wrong) origin; the true one
    # places input block 120 at its own RTP.
    for c, s in zip(caps, sp):
        assert c.counter_map.input_index(rtp_at(120, 51, olen_for(s.sample_rate))) == 120 * ILEN


def test_stream_is_built_from_what_radiod_granted():
    sp = specs()

    def downgraded(control, spec, n):
        r = reply_for(spec, 51, 100 + n)
        r.output_encoding = 2                                   # S16BE, not F32
        r.output_data_dest_socket = {"address": "239.9.9.9", "port": 5010}
        return r

    grants = []
    make_probe(FakeControl(sp, reply_fn=downgraded), grants=grants).run(sp, 5)
    assert len(grants) == 7
    assert all((g.encoding, g.address, g.port) == (2, "239.9.9.9", 5010) for _, g in grants)


def test_undecodable_granted_encoding_aborts_naming_the_clone_and_removes_all():
    sp = specs()

    def opus(control, spec, n):
        r = reply_for(spec, 51, 100 + n)
        r.output_encoding = 3
        return r

    control = FakeControl(sp, reply_fn=opus)
    with pytest.raises(RuntimeError, match="T6_REF.*cannot decode"):
        make_probe(control).run(sp, 5)
    assert sorted(control.removed) == sorted(s.ssrc for s in sp)


def test_a_live_ssrc_refuses_the_whole_run_before_anything_is_created():
    sp = specs()
    control = FakeControl(sp)
    control.live.add(sp[3].ssrc)
    with pytest.raises(ValueError, match=f"{sp[3].ssrc:#x}"):
        make_probe(control).run(sp, 5)
    assert control.created == [] and control.removed == []


def test_an_interrupt_mid_run_returns_what_was_gathered_and_removes_all():
    sp = specs()

    def interrupted(control, spec, n):
        if n >= 6:                         # grant poll is n=0; polling loop follows
            raise KeyboardInterrupt
        return reply_for(spec, 51, 100 + n)

    control = FakeControl(sp, reply_fn=interrupted)
    caps, epochs = make_probe(control).run(sp, 600)
    assert len(caps) == 7 and caps[0].counter_map is not None and caps[0].edges
    assert sorted(control.removed) == sorted(s.ssrc for s in sp)


def test_ka9q_stream_factory_opens_nothing_before_start(monkeypatch):
    import ka9q
    seen = {}

    class RecStream:
        def __init__(self, channel, on_samples, **kw):
            seen.update(channel=channel, kw=kw)

    monkeypatch.setattr(ka9q, "RadiodStream", RecStream)
    from hf_timestd.core.hf_chain_delay_probe import ChannelGrant, ka9q_stream_factory
    spec = specs()[0]
    stream = ka9q_stream_factory()(spec, lambda *a: None, ChannelGrant(2, "239.9.9.9", 5010))
    ch = seen["channel"]
    assert isinstance(stream, RecStream)
    assert (ch.ssrc, ch.sample_rate, ch.multicast_address, ch.port, ch.encoding) == (
        spec.ssrc, 96_000, "239.9.9.9", 5010, 2)
