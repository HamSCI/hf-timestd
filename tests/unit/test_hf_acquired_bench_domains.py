"""Task 8: the judge's T3 bench and the label anchor stay in one channel's
RTP counter.  Each radiod channel starts its own counter, so an arrival from
another channel is not a point in the counter the registration lives in."""
import types

import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.native_anchor import LabelAnchor, NativeAnchor
from hf_timestd.core.offset_judge import HfAcquiredBench
from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import RegistrationStore
from hf_timestd.core.stream_recorder_v2 import StreamRecorderV2

from synth_origins import F_IN, ILEN, olen_for, rtp_at

SR = 24000
OLEN = olen_for(SR)
BLOCK_S = ILEN / F_IN  # 20 ms


def _store(tmp_path, clock):
    return RegistrationStore(
        tmp_path / "reg", tmp_path / "registration.json", time_fn=lambda: clock[0]
    )


def _write(st, *, k0, utc_ref, ref_block, domain="SHARED_10000"):
    st.write_summary(
        Registration(
            "ep-1",
            rtp_ref=rtp_at(ref_block, k0, OLEN),
            utc_ref=utc_ref,
            sample_rate=SR,
            sigma_ms=0.8,
            channel="fused",
            verified=True,
            rtp_domain=domain,
        ),
        ["SHARED_10000", "WWV_20000"],
        "ACQUIRED",
        {},
    )


@pytest.mark.parametrize("other_k0", [50, 351], ids=["20ms-apart", "6s-apart"])
def test_bench_projects_through_the_summarys_own_channel(tmp_path, other_k0):
    clock = [5000.0]
    st = _store(tmp_path, clock)
    _write(st, k0=51, utc_ref=100.0, ref_block=100)
    arrive_block = 100 + 50
    truth_at_arrival = 100.0 + (arrive_block - 100) * BLOCK_S
    arrivals = {
        "SHARED_10000": (rtp_at(arrive_block, 51, OLEN), 42.0, SR),
        # newer, and in a counter whose origin is another block
        "WWV_20000": (rtp_at(arrive_block, other_k0, OLEN), 42.2, SR),
    }
    bench = HfAcquiredBench(
        provider=lambda: arrivals,
        store=st,
        mono_fn=lambda: 42.5,
        time_fn=lambda: clock[0],
    )
    reading = bench.poll()
    assert reading is not None
    assert reading.utc == pytest.approx(truth_at_arrival, abs=1e-6)
    assert reading.mono == 42.0


def test_bench_is_silent_without_a_domain_or_its_arrival(tmp_path):
    clock = [5000.0]
    st = _store(tmp_path, clock)
    arrive = rtp_at(150, 51, OLEN)

    def bench_for(arrivals):
        return HfAcquiredBench(
            provider=lambda: arrivals,
            store=st,
            mono_fn=lambda: 42.5,
            time_fn=lambda: clock[0],
        )

    # a summary that names no domain: nothing says which counter rtp_ref is in
    _write(st, k0=51, utc_ref=100.0, ref_block=100, domain="")
    assert bench_for({"SHARED_10000": (arrive, 42.0, SR)}).poll() is None
    # a summary naming a channel that has not arrived
    _write(st, k0=51, utc_ref=100.0, ref_block=100)
    assert bench_for({"WWV_20000": (arrive, 42.0, SR)}).poll() is None
    assert bench_for({}).poll() is None
    # sanity: with its own arrival it answers
    assert bench_for({"SHARED_10000": (arrive, 42.0, SR)}).poll() is not None


class _FakeStream:
    def __init__(self):
        self.tap = None

    def add_tap(self, cb):
        self.tap = cb


def test_tap_records_arrivals_per_channel():
    rec = CoreRecorderV2.__new__(CoreRecorderV2)
    a, b = _FakeStream(), _FakeStream()
    rec._wire_t5_fallback_arrival("SHARED_10000", a, SR)
    rec._wire_t5_fallback_arrival("WWV_20000", b, SR)
    q = lambda r: types.SimpleNamespace(  # noqa: E731
        last_rtp_timestamp=r, delivered_rtp_start=r, batch_samples_delivered=240
    )
    a.tap([0.0] * 240, q(1000))
    b.tap([0.0] * 240, q(5000))
    got = rec._hf_acquired_bench_state()
    assert set(got) == {"SHARED_10000", "WWV_20000"}
    assert got["SHARED_10000"][0] == 1240 and got["WWV_20000"][0] == 5240
    got.clear()  # a copy: the bench cannot disturb the recorder's record
    assert len(rec._hf_acquired_bench_state()) == 2


def test_t3_label_anchor_refused_on_another_channel_of_the_same_rate():
    anchor = NativeAnchor(
        anchor_rtp=1000,
        anchor_utc_ns=1_800_000_000_000_000_000,
        sample_rate_hz=SR,
        chain_delay_ns=0,
        captured_at_utc_ns=1_800_000_000_000_000_000,
        captured_via_tier="T3",
    )
    label = LabelAnchor(
        anchor=anchor, epoch_id="ep-1", tier="T3", sigma_ns=1e6,
        rtp_domain="SHARED_10000",
    )
    rec = StreamRecorderV2.__new__(StreamRecorderV2)
    rec.config = types.SimpleNamespace(description="WWV_20000", sample_rate=SR)
    rec._label_anchor_provider = lambda: label
    assert rec._label_anchor_state() is None
    rec.config.description = "SHARED_10000"
    assert rec._label_anchor_state() is label
    # a T6 anchor names no domain and keeps the rate guard alone
    t6 = LabelAnchor(anchor=anchor, epoch_id="e", tier="T6", sigma_ns=1e3)
    rec.config.description = "WWV_20000"
    rec._label_anchor_provider = lambda: t6
    assert rec._label_anchor_state() is t6
