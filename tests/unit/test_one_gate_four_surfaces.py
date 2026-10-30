"""ONE gate for every surface the registration reaches (fix round 1, C-1).

The review measured the hole.  `T3RegistrationAnchor` demanded
`state == "ACQUIRED"` and `verified is True`; `HfAcquiredBench.poll`
admitted ACQUIRED **or** WITNESS and never read `verified` at all -- and
the FUSE chrony block comes from the bench reading, not from the
`LabelAnchor` the other three surfaces share.  So:

    state=ACQUIRED  verified=False   ring/§18/sidecar refused, chrony FED
    state=WITNESS   verified=True    ring/§18/sidecar refused, chrony FED
    state=WITNESS   verified=False   ring/§18/sidecar refused, chrony FED

An unverified plane can sit on a fold-lattice phantom -- two channels can
share one lattice, and `FUSE_OUTLIER_MS` bounds disagreement between
members, not a common-mode lattice error.  A plane good enough to
discipline the host clock is a plane good enough to label the samples;
there is no honest reading on which those two differ.

`registration_store.registration_refusal` is now THE one gate, and this
file walks all four surfaces through the same three cases.
"""

import json

import pytest

from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns
from hf_timestd.core.offset_judge import (
    HfAcquiredBench,
    OffsetJudge,
    label_plane_chrony_sample,
)
from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import (
    ADOPT_MIN_CORROBORATED_MINUTES,
    RegistrationStore,
    registration_is_authoritative,
    registration_refusal,
)
from hf_timestd.core.t3_registration_anchor import T3RegistrationAnchor

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)
RTP_REF = 1000


def _store(
    tmp_path,
    clock,
    state,
    *,
    verified,
    sigma_ms=1.0,
    n_minutes=ADOPT_MIN_CORROBORATED_MINUTES,
):
    """task 16b: ``n_minutes`` defaults to the adoption floor, so every
    case below still means what it meant before the corroboration gate
    existed."""
    st = RegistrationStore(
        tmp_path / "reg",
        tmp_path / "registration.json",
        time_fn=lambda: clock[0],
    )
    st.write_summary(
        Registration(
            "ep-1",
            rtp_ref=RTP_REF,
            utc_ref=WALL0,
            sample_rate=SR,
            sigma_ms=sigma_ms,
            n_minutes=n_minutes,
            channel="fused",
            rtp_domain="SHARED_10000",
            verified=verified,
        ),
        ["SHARED_10000", "WWV_15000"],
        state,
        {"counter_epoch_id": "ep-1"},
    )
    return st


# ── the gate itself ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state,verified,expected",
    [
        ("ACQUIRED", True, None),
        ("ACQUIRED", False, "unverified"),
        ("WITNESS", True, "state:WITNESS"),
        ("WITNESS", False, "state:WITNESS"),
        ("CANDIDATE", True, "state:CANDIDATE"),
        ("BOOTSTRAP", False, "state:BOOTSTRAP"),
        ("CONFLICT", True, "state:CONFLICT"),
    ],
)
def test_the_gate_names_its_refusal(tmp_path, state, verified, expected):
    clock = [WALL0]
    summary = _store(tmp_path, clock, state, verified=verified).read_summary()
    assert registration_refusal(summary, now=WALL0, sample_rate=SR) == expected
    assert registration_is_authoritative(summary, now=WALL0, sample_rate=SR) is (
        expected is None
    )


@pytest.mark.parametrize(
    "n_minutes,expected",
    [(0, "uncorroborated"), (1, "uncorroborated"), (2, None), (17, None)],
)
def test_the_gate_waits_for_two_corroborated_minutes(tmp_path, n_minutes, expected):
    """task 16b: on ND at 20:36Z a plane in its FIRST minute anchored the
    ring and steered chrony, and corroboration only threw it out six
    minutes later.  ADOPT_MIN_CORROBORATED_MINUTES minutes of
    corroboration now stand between an acquisition and any surface
    acting on it."""
    clock = [WALL0]
    summary = _store(
        tmp_path, clock, "ACQUIRED", verified=True, n_minutes=n_minutes
    ).read_summary()
    assert registration_refusal(summary, now=WALL0, sample_rate=SR) == expected


def test_an_unparseable_n_minutes_is_incomplete(tmp_path):
    clock = [WALL0]
    summary = _store(tmp_path, clock, "ACQUIRED", verified=True).read_summary()
    assert registration_refusal(dict(summary, n_minutes="soon"), now=WALL0) == (
        "incomplete"
    )
    # absent entirely: fail closed on nothing corroborated
    del summary["n_minutes"]
    assert registration_refusal(summary, now=WALL0) == "uncorroborated"


def test_the_gate_refuses_a_missing_stale_or_foreign_summary(tmp_path):
    clock = [WALL0]
    st = _store(tmp_path, clock, "ACQUIRED", verified=True)
    summary = st.read_summary()
    assert registration_refusal(None, now=WALL0) == "no_summary"
    assert registration_refusal({}, now=WALL0) == "no_summary"
    assert registration_refusal(summary, now=WALL0 + 301.0) == "stale"
    assert (
        registration_refusal(summary, now=WALL0, sample_rate=96000)
        == "sample_rate_mismatch"
    )
    assert registration_refusal(dict(summary, rtp_ref=None), now=WALL0) == "incomplete"


def test_state_is_checked_before_the_fields(tmp_path):
    """A BOOTSTRAP summary carries null fields; it must be refused for
    its state, not mislabelled `incomplete`."""
    clock = [WALL0]
    st = RegistrationStore(
        tmp_path / "reg",
        tmp_path / "registration.json",
        time_fn=lambda: clock[0],
    )
    st.write_summary(None, [], "BOOTSTRAP", {})
    assert registration_refusal(st.read_summary(), now=WALL0) == "state:BOOTSTRAP"


# ── all four surfaces, one predicate ─────────────────────────────────


def _anchor_surface(store, clock):
    """Surfaces 1-3: ring, §18 and sidecar all label from this object."""
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    decision = holder.refresh(t6_authoritative=False)
    return holder.state(), decision.reason


def _fuse_surface(store, clock, tmp_path):
    """Surface 4: the judge's label_plane_anchor -> the chrony sample."""
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (RTP_REF, 1000.0, SR)},
        store=store,
        mono_fn=lambda: 1000.0,
        time_fn=lambda: clock[0],
    )
    judge = OffsetJudge(
        config={"enabled": True},
        benches=[bench],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: clock[0],
        mono_fn=lambda: 1000.0,
        # Task 17a: these tests are ABOUT the anchor closure, which is now
        # opt-in and off by default, so they opt in explicitly.
        anchor_closure=True,
        t3_admitted=True,
    )
    judge.register_radiod_pair(
        KEY,
        unix_ns_to_gps_time_ns(int(round((WALL0 + 0.150) * 1e9))),
        RTP_REF,
        SR,
    )
    judge.tick()
    published = json.loads((tmp_path / "offset_judge.json").read_text())
    sample = label_plane_chrony_sample(
        tmp_path / "offset_judge.json",
        time_fn=lambda: clock[0] + 0.150,
        mono_fn=lambda: 1000.0,
    )
    return bench.poll(), published["label_plane_anchor"], sample


def test_acquired_and_verified_reaches_all_four_surfaces(tmp_path):
    clock = [WALL0]
    store = _store(tmp_path, clock, "ACQUIRED", verified=True)
    label, reason = _anchor_surface(store, clock)
    assert reason == "acquired" and label is not None and label.tier == "T3"
    reading, block, sample = _fuse_surface(store, clock, tmp_path)
    assert reading is not None and reading.detail["authoritative"] is True
    assert block is not None and block["bench"] == "hf_acquired"
    assert sample is not None
    assert sample.offset_s == pytest.approx(-0.150, abs=1e-6)


def test_acquired_but_unverified_reaches_none_of_them(tmp_path):
    """The measured hole.  Before the fix chrony was FED at -150.0 ms
    while the other three surfaces refused the same plane."""
    clock = [WALL0]
    store = _store(tmp_path, clock, "ACQUIRED", verified=False)
    label, reason = _anchor_surface(store, clock)
    assert label is None and reason == "unverified"
    reading, block, sample = _fuse_surface(store, clock, tmp_path)
    assert reading is None  # not even a witness reading
    assert block is None
    assert sample is None


@pytest.mark.parametrize("verified", [True, False])
def test_a_witness_plane_witnesses_and_never_steers(tmp_path, verified):
    """WITNESS means a T6 station publishes the acquired plane without it
    driving metrology.  The bench must still answer -- the judge needs the
    hf_acquired-vs-T6 residual -- but the FUSE feed must not use it: on a
    T6 station the label plane is T6's own, or none."""
    clock = [WALL0]
    store = _store(tmp_path, clock, "WITNESS", verified=verified)
    label, reason = _anchor_surface(store, clock)
    assert label is None and reason == "state:WITNESS"
    reading, block, sample = _fuse_surface(store, clock, tmp_path)
    if verified:
        assert reading is not None  # witness reading
        assert reading.detail["authoritative"] is False
        assert reading.detail["refusal"] == "state:WITNESS"
    else:
        assert reading is None  # unverified: silent
    assert block is None
    assert sample is None


@pytest.mark.parametrize("n_minutes", [0, 1])
def test_an_uncorroborated_plane_reaches_none_of_them(tmp_path, n_minutes):
    """task 16b: verified once, corroborated less than
    ADOPT_MIN_CORROBORATED_MINUTES times.  On ND at 20:36Z exactly this
    plane anchored the ring and steered chrony 23-28 ms off an NTP
    consensus of 5-13 ms."""
    clock = [WALL0]
    store = _store(tmp_path, clock, "ACQUIRED", verified=True, n_minutes=n_minutes)
    label, reason = _anchor_surface(store, clock)
    assert label is None and reason == "uncorroborated"
    reading, block, sample = _fuse_surface(store, clock, tmp_path)
    # the bench still answers (the judge wants the residual) but the
    # reading is not authoritative, so no label plane and no chrony sample
    assert reading is not None
    assert reading.detail["authoritative"] is False
    assert reading.detail["refusal"] == "uncorroborated"
    assert block is None
    assert sample is None


def test_a_stale_plane_reaches_none_of_them(tmp_path):
    clock = [WALL0]
    store = _store(tmp_path, clock, "ACQUIRED", verified=True)
    clock[0] += 301.0
    label, reason = _anchor_surface(store, clock)
    assert label is None and reason == "stale"
    reading, block, sample = _fuse_surface(store, clock, tmp_path)
    assert reading is None and block is None and sample is None


def test_a_t6_native_reading_needs_no_authoritative_key(tmp_path):
    """T6's own authority gate governs NativeAnchorBench, so a reading
    without the key defaults to authoritative -- the FUSE feed must not
    go silent on a T6 station just because T3 stamps a flag T6 does not."""
    from hf_timestd.core.offset_judge import BenchReading

    class _Fixed:
        def __init__(self, r):
            self._r = r

        def poll(self):
            return self._r

    t6 = BenchReading(
        tier="T6",
        utc=WALL0,
        sigma_ns=5e4,
        mono=1000.0,
        detail={"anchor_tier": "T6"},
        plane="label",
    )
    judge = OffsetJudge(
        config={"enabled": True},
        benches=[_Fixed(t6)],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: WALL0,
        mono_fn=lambda: 1000.0,
        # Task 17a: these tests are ABOUT the anchor closure, which is now
        # opt-in and off by default, so they opt in explicitly.
        anchor_closure=True,
        t3_admitted=True,
    )
    judge.register_radiod_pair(
        KEY, unix_ns_to_gps_time_ns(int(WALL0 * 1e9)), RTP_REF, SR
    )
    judge.tick()
    block = json.loads((tmp_path / "offset_judge.json").read_text())[
        "label_plane_anchor"
    ]
    assert block is not None and block["tier"] == "T6"
