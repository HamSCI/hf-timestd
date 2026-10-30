import pytest

from hf_timestd.core.offset_judge import (
    BenchReading,
    HfAcquiredBench,
    OffsetJudge,
    gps_time_ns_to_unix,
)
from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import RegistrationStore
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2

SR = 24000


def _store(tmp_path, clock):
    return RegistrationStore(
        tmp_path / "reg", tmp_path / "registration.json", time_fn=lambda: clock[0]
    )


def test_bench_projects_the_registration_to_the_arrival(tmp_path):
    clock = [5000.0]
    st = _store(tmp_path, clock)
    st.write_summary(
        Registration(
            "ep-1",
            rtp_ref=1000,
            utc_ref=100.0,
            sample_rate=SR,
            sigma_ms=0.8,
            channel="fused",
            rtp_domain="SHARED_10000",
            verified=True,
        ),
        ["SHARED_10000"],
        "ACQUIRED",
        {"raw_pair_residual_ms": 16.7},
    )
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (1000 + 10 * SR, 42.0, SR)},
        store=st,
        mono_fn=lambda: 42.5,
        time_fn=lambda: clock[0],
    )
    r = bench.poll()
    assert r is not None
    assert r.utc == 110.0 and r.mono == 42.0
    # addendum: the bench floors its sigma at ORIGIN_SIGMA_FLOOR_MS -- the
    # summary's 0.8 ms is the fused plane's repeatability, and the bench's
    # sigma is an ACCURACY claim
    assert r.sigma_ns == 1.0e6 and r.tier == "T3"
    assert (
        r.detail["bench"] == "hf_acquired" and r.detail["raw_pair_residual_ms"] == 16.7
    )
    assert r.plane == "label"


def test_bench_answers_on_witness_state_too(tmp_path):
    """A T6 station publishes WITNESS: the acquired plane exists but does
    not drive metrology.  The bench still answers so the judge gets the
    hf_acquired-vs-T6 residual (controller ruling, task 9)."""
    clock = [5000.0]
    st = _store(tmp_path, clock)
    st.write_summary(
        Registration(
            "ep-1",
            rtp_ref=1000,
            utc_ref=100.0,
            sample_rate=SR,
            sigma_ms=0.8,
            channel="fused",
            rtp_domain="SHARED_10000",
            verified=True,
        ),
        ["SHARED_10000"],
        "WITNESS",
        {"raw_pair_residual_ms": 4.2},
    )
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (1000 + 10 * SR, 42.0, SR)},
        store=st,
        mono_fn=lambda: 42.5,
        time_fn=lambda: clock[0],
    )
    r = bench.poll()
    assert r is not None
    assert r.utc == 110.0 and r.mono == 42.0
    assert r.sigma_ns == 1.0e6 and r.tier == "T3"  # floored, see above
    assert (
        r.detail["bench"] == "hf_acquired" and r.detail["raw_pair_residual_ms"] == 4.2
    )


def test_bench_silent_in_bootstrap_or_when_stale(tmp_path):
    clock = [5000.0]
    st = _store(tmp_path, clock)
    st.write_summary(None, [], "BOOTSTRAP", {})
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (1, 1.0, SR)},
        store=st,
        mono_fn=lambda: 1.0,
        time_fn=lambda: clock[0],
    )
    assert bench.poll() is None
    st.write_summary(
        Registration("ep-1", 1000, 100.0, SR, 0.8, channel="fused", verified=True,
                     rtp_domain="SHARED_10000"),
        ["x"],
        "ACQUIRED",
        {},
    )
    clock[0] += 200.0
    assert bench.poll() is None
    assert HfAcquiredBench(provider=lambda: None, store=st).poll() is None


def test_bench_silent_on_sample_rate_mismatch(tmp_path):
    """Fix round 1 (task 9 review F1/F2): the registration was acquired
    on a 24 kHz archive channel.  An arrival stamped from a DIFFERENT
    counter domain (e.g. the 4 kHz WWVB stream, or the 96 kHz T6
    stream) must not be projected against rtp_ref -- the naive
    delta-rtp/sample_rate arithmetic would silently publish a wrong
    utc.  Silence beats a wrong answer."""
    clock = [5000.0]
    st = _store(tmp_path, clock)
    st.write_summary(
        Registration(
            "ep-1",
            rtp_ref=1000,
            utc_ref=100.0,
            sample_rate=SR,
            sigma_ms=0.8,
            channel="fused",
            rtp_domain="SHARED_10000",
            verified=True,
        ),
        ["SHARED_10000"],
        "ACQUIRED",
        {"raw_pair_residual_ms": 16.7},
    )
    # WWVB-shaped arrival: same nominal rtp delta, WRONG (4 kHz) domain.
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (1000 + 10 * SR, 42.0, 4000)},
        store=st,
        mono_fn=lambda: 42.5,
        time_fn=lambda: clock[0],
    )
    assert bench.poll() is None

    # T6-shaped arrival: WRONG (96 kHz) domain.
    bench_t6 = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (1000 + 10 * SR, 42.0, 96000)},
        store=st,
        mono_fn=lambda: 42.5,
        time_fn=lambda: clock[0],
    )
    assert bench_t6.poll() is None

    # Sanity: the SAME arrival at the matching rate does answer.
    bench_ok = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (1000 + 10 * SR, 42.0, SR)},
        store=st,
        mono_fn=lambda: 42.5,
        time_fn=lambda: clock[0],
    )
    assert bench_ok.poll() is not None


def _unix_to_gps_ns(unix_s: float) -> int:
    """Invert gps_time_ns_to_unix without hardcoding the leap count."""
    guess = int((unix_s - 315964800) * 1e9)
    back = gps_time_ns_to_unix(guess)
    return guess + int(round((unix_s - back) * 1e9))


class _FixedBench:
    """A bench stub whose poll() always returns the same BenchReading."""

    def __init__(self, reading):
        self._reading = reading

    def poll(self):
        return self._reading


def test_same_tier_arbitration_prefers_smaller_sigma(tmp_path):
    """F3 (task 9 review): FusionBench and HfAcquiredBench both publish
    tier "T3".  _select_bench_locked's sort used to be a plain
    tier-rank sort, so Python's stable sort silently preferred whichever
    bench was constructed/registered FIRST on every tie -- FusionBench,
    since it is part of the judge's default bench list and
    HfAcquiredBench is always add_bench()'d afterward.  The documented
    rule (CLAUDE.md: "tier rank never substitutes for demonstrated
    precision") is that the tighter reading wins regardless of
    registration order.  Here the LOOSE reading is registered first
    (FusionBench-shaped) and the TIGHT one later (HfAcquiredBench-shaped,
    via add_bench) -- the tight, later one must still be selected."""
    wall0 = 1_800_000_000.0
    clock = {"wall": wall0, "mono": 1000.0}

    loose = BenchReading(tier="T3", utc=wall0, sigma_ns=25_000_000.0, mono=1000.0)
    tight = BenchReading(tier="T3", utc=wall0, sigma_ns=1_000_000.0, mono=1000.0)

    judge = OffsetJudge(
        config={"enabled": True},
        benches=[_FixedBench(loose)],  # registered FIRST, looser
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: clock["wall"],
        mono_fn=lambda: clock["mono"],
    )
    judge.add_bench(_FixedBench(tight))  # registered LATER, tighter

    key = ("hf-status.local", 0xABCD1234)
    judge.register_radiod_pair(key, _unix_to_gps_ns(wall0), 0, SR)
    judge.tick()

    verdict = judge.offset_for(key, 0)
    assert verdict is not None
    assert verdict.tier == "T3"
    assert verdict.sigma_ns == tight.sigma_ns


class _FakeStreamRecorder:
    """Stands in for StreamRecorderV2: only add_tap() is exercised by
    _wire_t5_fallback_arrival."""

    def __init__(self):
        self.tap = None

    def add_tap(self, callback):
        self.tap = callback


class _FakeQuality:
    def __init__(
        self, last_rtp_timestamp, delivered_rtp_start=None, batch_samples_delivered=0
    ):
        self.last_rtp_timestamp = last_rtp_timestamp
        if delivered_rtp_start is not None:
            self.delivered_rtp_start = delivered_rtp_start
            self.batch_samples_delivered = batch_samples_delivered


def test_wire_t5_fallback_arrival_feeds_hf_arrival_from_the_real_tap():
    """Recorder-level coverage (task 9 review ruling: cover the
    provider from the ACTUAL production hook when it can be built
    cheaply).  CoreRecorderV2 cannot be constructed via its normal
    __init__ in a unit test (radiod/ka9q/config dependencies), so this
    uses the same __new__-bypass pattern already established in this
    test suite (see test_wwvb_rtp_probe.py) -- but exercises the REAL
    _wire_t5_fallback_arrival + the REAL tap it installs, with only the
    StreamRecorderV2 and StreamQuality objects faked out.  This is the
    one and only site that writes self._hf_arrival (fix round 1)."""
    recorder = CoreRecorderV2.__new__(CoreRecorderV2)
    fake_stream = _FakeStreamRecorder()

    recorder._wire_t5_fallback_arrival("SHARED_10000", fake_stream, SR)
    assert fake_stream.tap is not None

    samples = [0.0] * 240
    fake_stream.tap(
        samples,
        _FakeQuality(
            last_rtp_timestamp=1000,
            delivered_rtp_start=1000,
            batch_samples_delivered=240,
        ),
    )

    arrivals = recorder._hf_acquired_bench_state()
    assert set(arrivals) == {"SHARED_10000"}
    arrival = arrivals["SHARED_10000"]
    arrival_rtp, arrival_mono, arrival_sr = arrival
    assert arrival_rtp == 1000 + 240
    assert arrival_sr == SR
    assert isinstance(arrival_mono, float) and arrival_mono > 0


def test_arrival_tap_labels_from_the_delivered_stream_not_the_received_header():
    """I1 (final review): the tap hand-rolled ``last_rtp_timestamp +
    len(samples)``.  ``last_rtp_timestamp`` is the last RECEIVED packet's
    header, stamped before the resequencer runs, and it "desynchronizes
    from delivered samples under loss" -- the root cause of the T6 origin
    slips of 2026-08-11.  ``newest_sample_rtp`` exists and returns exactly
    this quantity from ``delivered_rtp_start + batch_samples_delivered``.
    HfAcquiredBench pairs the label with time.monotonic() and projects the
    registration onto it, so any slip lands directly on the bench's utc."""
    recorder = CoreRecorderV2.__new__(CoreRecorderV2)
    fake_stream = _FakeStreamRecorder()
    recorder._wire_t5_fallback_arrival("SHARED_10000", fake_stream, SR)
    # loss: the received header ran 4800 samples (200 ms) ahead of what the
    # resequencer actually delivered
    fake_stream.tap(
        [0.0] * 240,
        _FakeQuality(
            last_rtp_timestamp=1000 + 4800,
            delivered_rtp_start=1000,
            batch_samples_delivered=240,
        ),
    )
    arrival_rtp, _mono, _sr = recorder._hf_acquired_bench_state()["SHARED_10000"]
    assert arrival_rtp == 1240
    assert arrival_rtp != (1000 + 4800) + 240  # the hand-rolled label


def test_arrival_tap_falls_back_for_a_producer_without_the_delivered_field():
    """``newest_sample_rtp``'s own fallback: ka9q-python < 3.21.0 has no
    ``delivered_rtp_start``, so the received header is all there is."""
    recorder = CoreRecorderV2.__new__(CoreRecorderV2)
    fake_stream = _FakeStreamRecorder()
    recorder._wire_t5_fallback_arrival("SHARED_10000", fake_stream, SR)
    fake_stream.tap([0.0] * 240, _FakeQuality(last_rtp_timestamp=1000))
    arrival_rtp, _mono, _sr = recorder._hf_acquired_bench_state()["SHARED_10000"]
    assert arrival_rtp == 1000


def test_wwvb_stream_no_longer_writes_hf_arrival():
    """Fix round 1 (F1/F2): the WWVB (4 kHz) path must NOT feed
    _hf_arrival -- it is a different RTP counter domain than the 24 kHz
    archive channel the registration is acquired against.  A bare
    recorder (no archive tap ever wired) has no _hf_arrival at all."""
    recorder = CoreRecorderV2.__new__(CoreRecorderV2)
    assert recorder._hf_acquired_bench_state() is None


# ── addendum: the bench's sigma is an ACCURACY claim, floored ─────────


def _acquired_summary(tmp_path, clock, sigma_ms, utc_ref=100.0):
    st = _store(tmp_path, clock)
    st.write_summary(
        Registration(
            "ep-1",
            rtp_ref=0,
            utc_ref=utc_ref,
            sample_rate=SR,
            sigma_ms=sigma_ms,
            channel="fused",
            rtp_domain="SHARED_10000",
            verified=True,
        ),
        ["SHARED_10000", "WWV_20000"],
        "ACQUIRED",
        {},
    )
    return st


@pytest.mark.parametrize(
    "sigma_ms,expected_ns",
    [
        (0.41, 1.0e6),  # six 1 ms channels fused: repeatability, not accuracy
        (0.707, 1.0e6),  # two 1 ms channels fused
        (1.0, 1.0e6),  # already at the bound
        (2.5, 2.5e6),  # a loose plane is published as loose
    ],
)
def test_bench_sigma_is_floored_at_the_delay_model_bound(
    tmp_path, sigma_ms, expected_ns
):
    """The delay-model accuracy bound is COMMON-MODE across channels: every
    channel's origin is `expected_delay - fold_position`, so they all
    inherit the same great-circle/F2-hop model error and the same mode
    ambiguity, and inverse-variance combination across N channels does not
    shrink it.  `fuse_registrations` is right to report the fused plane's
    repeatability (that is what its inputs measure), so the floor belongs
    here, where the number becomes an accuracy claim the judge acts on."""
    clock = [5000.0]
    st = _acquired_summary(tmp_path, clock, sigma_ms)
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (0, 42.0, SR)},
        store=st,
        mono_fn=lambda: 42.0,
        time_fn=lambda: clock[0],
    )
    r = bench.poll()
    assert r is not None
    assert r.sigma_ns == pytest.approx(expected_ns)


def test_floored_hf_acquired_does_not_displace_a_tighter_fusion_bench(tmp_path):
    """The reason the floor is load-bearing.  FusionBench and
    HfAcquiredBench are the only pair sharing tier T3, so
    _select_bench_locked's same-tier sigma tie-break decides which of them
    governs -- and the judge's offset_ns is what the recorder folds into
    the ring anchor and what reaches chrony through FUSE.  A fused
    registration presenting 0.41 ms of repeatability must not take T3 from
    a FusionBench reading at 0.6 ms; floored to the delay-model bound it
    cannot."""
    wall0 = 1_800_000_000.0
    clock = [wall0]
    st = _acquired_summary(tmp_path, clock, 0.41, utc_ref=wall0)
    fusion_like = BenchReading(tier="T3", utc=wall0, sigma_ns=600_000.0, mono=1000.0)
    judge = OffsetJudge(
        config={"enabled": True},
        benches=[_FixedBench(fusion_like)],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: wall0,
        mono_fn=lambda: 1000.0,
    )
    judge.add_bench(
        HfAcquiredBench(
            provider=lambda: {"SHARED_10000": (0, 1000.0, SR)},
            store=st,
            mono_fn=lambda: 1000.0,
            time_fn=lambda: wall0,
        )
    )
    key = ("hf-status.local", 0xABCD1234)
    judge.register_radiod_pair(key, _unix_to_gps_ns(wall0), 0, SR)
    judge.tick()
    verdict = judge.offset_for(key, 0)
    assert verdict is not None and verdict.tier == "T3"
    assert verdict.sigma_ns == pytest.approx(600_000.0)
