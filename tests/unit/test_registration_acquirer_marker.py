import pytest

from hf_timestd.core.registration_acquirer import (
    integer_second_correction,
    locate_minute_marker,
)
from synth_ticks import make_tick_audio

SR = 24000
MIN = 1_800_000_000
T0 = MIN - 4.0  # 10 s buffer: the ±1.5 s marker search must fit for walks up to ±1.2 s


@pytest.mark.parametrize("walk_s", [0.0, 0.4, -0.7, 1.2])
def test_marker_found_where_truth_put_it(walk_s):
    audio = make_tick_audio(10, SR, T0, {"WWV": 0.012}, snr_db=15.0)
    res = locate_minute_marker(audio, SR, T0 + walk_s, "1000", MIN)
    assert res is not None
    offset_s, snr = res
    # in the label frame the marker onset sits at d + walk after the minute
    assert offset_s == pytest.approx(0.012 + walk_s, abs=0.005)
    assert snr > 6.0


def test_no_marker_returns_none():
    audio = make_tick_audio(10, SR, T0, {"WWV": 0.012}, snr_db=15.0, marker=False)
    assert locate_minute_marker(audio, SR, T0, "1000", MIN) is None


@pytest.mark.parametrize("snr_db", [30.0, 40.0])
def test_no_marker_at_high_tick_snr(snr_db):
    # strong ticks must not leak past the gate as the tick SNR rises --
    # a windowed-MEAN score's own MAD shrinks as fast as the leak it is
    # trying to detect above, which is why the score statistic is a
    # bin-median, not a mean-over-MAD ratio.
    audio = make_tick_audio(10, SR, T0, {"WWV": 0.012}, snr_db=snr_db, marker=False)
    assert locate_minute_marker(audio, SR, T0, "1000", MIN) is None


def test_window_does_not_fit_returns_none():
    audio = make_tick_audio(10, SR, T0, {"WWV": 0.012}, snr_db=15.0)
    # the minute sits only 0.5 s after the label's sample0 -- the
    # ±1.5 s search cannot fit before it
    label = MIN - 0.5
    assert locate_minute_marker(audio, SR, label, "1000", MIN) is None


@pytest.mark.parametrize(
    "walk_s,frac,expected",
    [
        (
            0.4,
            -0.4,
            0,
        ),  # label 0.4 s late: total correction -0.4, fold says -0.4, no whole second
        (
            1.2,
            -0.2,
            -1,
        ),  # label 1.2 s late: total -1.2, fold wraps to -0.2, marker adds -1
        (
            -0.7,
            -0.3,
            1,
        ),  # label 0.7 s early: total +0.7, fold wraps to -0.3, marker adds +1
    ],
)
def test_integer_second_from_marker(walk_s, frac, expected):
    # the fold only ever reports (-0.5, 0.5]; the marker supplies the rest
    # marker_offset = d + walk ; total correction = -walk ; integer = round(-walk - frac)
    d = 0.012
    assert integer_second_correction(d + walk_s, d, frac) == expected


# ── Task 15: the 800 ms marker excludes BPM ───────────────────────────
#
# A shared channel that folds ONE 1000 Hz peak carries two hypotheses --
# WWV or BPM, 34 ms apart on ND -- and no sibling channel can choose
# between them when the dedicated channels hear no tick (AC0G-ND
# 2026-09-07 20:34Z: BOOTSTRAP indefinitely, so the anchor closure had
# nothing to anchor to).  WWV and WWVH transmit an 800 ms tone at second
# 0; BPM transmits no minute marker at all.  An 800 ms tone standing on
# the same fold position as the ticks therefore names their source.

D_SHARED = {"WWV": 0.010, "BPM": 0.044}
RUN_UP_S = 3.0  # the +-1.5 s marker search needs run-up before the minute


def _shared_channel_minute(walk_s, marker, station="WWV", snr_db=20.0, seed=7):
    """One 62 s buffer starting RUN_UP_S before the minute, so the marker
    search fits inside this single buffer."""
    from synth_ticks import label_timing

    t0 = MIN - RUN_UP_S
    audio = make_tick_audio(
        62,
        SR,
        t0,
        {station: D_SHARED[station]},
        snr_db=snr_db,
        seed=seed,
        marker=marker,
    )
    return audio, label_timing(t0, walk_s, SR), t0


def test_marker_names_wwv_on_a_shared_channel():
    """The acceptance case: one WWV tick train on a shared channel, both
    WWV and BPM eligible, no sibling.  The marker names WWV."""
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    acq = RegistrationAcquirer("SHARED_10000", SR)
    audio, label, t0 = _shared_channel_minute(walk_s=0.100, marker=True)
    reg = acq.offer_minute(audio, label, 1_000_000, MIN, D_SHARED, "ep-1")
    assert reg is not None, "the 800 ms marker should have named WWV"
    assert acq.state == acq.STATE_ACQUIRED
    assert reg.stations == ("WWV",)
    assert reg.hypotheses_open == 0
    assert acq._open == []
    assert reg.verified is False  # still a CANDIDATE: gate (b) has not run
    assert "marker" in reg.method
    # correction == -walk: the plane lands on truth
    assert reg.sample0_utc_for(1_000_000) == pytest.approx(t0, abs=0.003)


def test_no_marker_leaves_both_hypotheses_open():
    """Same audio without the marker: today's behaviour, unchanged."""
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    acq = RegistrationAcquirer("SHARED_10000", SR)
    audio, label, _t0 = _shared_channel_minute(walk_s=0.100, marker=False)
    assert acq.offer_minute(audio, label, 1_000_000, MIN, D_SHARED, "ep-1") is None
    assert acq.state == acq.STATE_BOOTSTRAP
    # task 16a: the BPM reading of the same peak no longer survives as a
    # hypothesis; one open WWV reading remains, ambiguous because band
    # 1000 admits BPM as a READING of that peak
    assert len(acq._open) == 1


def test_bpm_like_train_stays_bootstrap():
    """A train at BPM's delay with no 800 ms marker (BPM's own minute
    structure) must NOT be promoted -- nothing names it, and the marker
    rule may not invent a station."""
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    acq = RegistrationAcquirer("SHARED_10000", SR)
    audio, label, _t0 = _shared_channel_minute(
        walk_s=0.100, marker=False, station="BPM"
    )
    assert acq.offer_minute(audio, label, 1_000_000, MIN, D_SHARED, "ep-1") is None
    assert acq.state == acq.STATE_BOOTSTRAP
    # task 16a: the BPM reading of the same peak no longer survives as a
    # hypothesis; one open WWV reading remains, ambiguous because band
    # 1000 admits BPM as a READING of that peak
    assert len(acq._open) == 1


def test_live_minute_geometry_needs_a_second_minute():
    """The live ring hands the acquirer [minute, minute + 60 s), so the
    FIRST minute's own marker has no 1.5 s of run-up ahead of it and the
    search cannot fit.  The marker becomes searchable once a second
    minute is buffered, because ``_try_acquire`` folds and searches the
    CONCATENATED buffer in the frame of the oldest label -- where the
    second minute's marker sits 60 s in, with run-up on both sides."""
    from synth_ticks import label_timing
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    acq = RegistrationAcquirer("SHARED_10000", SR)
    got = None
    for k in range(2):
        t0 = MIN + 60 * k  # buffer starts exactly at the minute, as the ring does
        audio = make_tick_audio(
            62, SR, t0, {"WWV": D_SHARED["WWV"]}, snr_db=20.0, seed=7 + k
        )
        got = acq.offer_minute(
            audio,
            label_timing(t0, 0.100, SR),
            1_000_000 + k * 60 * SR,
            MIN + 60 * k,
            D_SHARED,
            "ep-1",
        )
        if k == 0:
            assert got is None, "one live-geometry minute cannot reach its own marker"
            assert len(acq._open) == 1
    assert got is not None, "the second minute puts a marker inside the search window"
    assert got.stations == ("WWV",)
    assert got.sample0_utc_for(1_000_000) == pytest.approx(MIN, abs=0.003)


# ── the naming rule itself, on hand-built hypotheses ──────────────────


def _hyp(station, band, correction_s, position_s, snr_db=30.0):
    from hf_timestd.core.registration_acquirer import Hypothesis

    return Hypothesis(
        correction_s=correction_s,
        sigma_ms=1.0,
        assignments=((station, band, position_s, snr_db),),
        support=1,
        unambiguous=False,
    )


def test_marker_names_wwvh_in_the_1200_band():
    """Symmetric rule: a marker in the 1200 band names WWVH."""
    from hf_timestd.core.registration_acquirer import marker_names_one_hypothesis

    d = {"WWVH": 0.023, "BPM": 0.041}
    # one 1200-band peak at 0.123 s: WWVH's correction is 0.023 - 0.123
    h_wwvh = _hyp("WWVH", "1200", correction_s=0.023 - 0.123, position_s=0.123)
    named = marker_names_one_hypothesis(
        [h_wwvh], d, lambda band: (0.1235, 22.0) if band == "1200" else None
    )
    assert named is not None
    assert named[0] is h_wwvh and named[1] == "1200"


def test_marker_off_every_hypothesis_names_nothing():
    """A marker that stands nowhere near the folded ticks belongs to
    something else (a carrier, a neighbouring station) -- hypotheses stay
    open."""
    from hf_timestd.core.registration_acquirer import marker_names_one_hypothesis

    h_wwv = _hyp("WWV", "1000", correction_s=0.010 - 0.080, position_s=0.080)
    h_bpm = _hyp("BPM", "1000", correction_s=0.044 - 0.080, position_s=0.080)
    assert (
        marker_names_one_hypothesis(
            [h_wwv, h_bpm], D_SHARED, lambda band: (0.140, 20.0)
        )
        is None
    )


def test_marker_on_the_ticks_excludes_bpm():
    """Both hypotheses come from ONE peak, so both predict the SAME
    marker position -- the position check confirms the marker belongs to
    this tick train, and BPM falls because BPM transmits no marker."""
    from hf_timestd.core.registration_acquirer import marker_names_one_hypothesis

    h_wwv = _hyp("WWV", "1000", correction_s=0.010 - 0.080, position_s=0.080)
    h_bpm = _hyp("BPM", "1000", correction_s=0.044 - 0.080, position_s=0.080)
    named = marker_names_one_hypothesis(
        [h_wwv, h_bpm], D_SHARED, lambda band: (0.0805, 31.0)
    )
    assert named is not None
    h, band, offset_s, snr_db, excluded = named
    assert h is h_wwv and band == "1000"
    assert offset_s == pytest.approx(0.0805) and snr_db == 31.0
    assert excluded == ("BPM",)


def test_absent_marker_names_nothing():
    from hf_timestd.core.registration_acquirer import marker_names_one_hypothesis

    h_wwv = _hyp("WWV", "1000", correction_s=0.010 - 0.080, position_s=0.080)
    h_bpm = _hyp("BPM", "1000", correction_s=0.044 - 0.080, position_s=0.080)
    assert (
        marker_names_one_hypothesis([h_wwv, h_bpm], D_SHARED, lambda band: None) is None
    )


# ── Task 16a at the acquirer: two 1000 Hz peaks, one marker ───────────


def _two_train_minute(marker, walk_s=0.100):
    """One WWV train (marker optional) plus a second 1000 Hz train 34 ms
    later carrying none -- the ND 20:36Z geometry.  The second train's
    own call runs at 60 dB so it adds ticks without adding noise."""
    from synth_ticks import label_timing

    t0 = MIN - RUN_UP_S
    wwv = make_tick_audio(
        62, SR, t0, {"WWV": 0.010}, snr_db=20.0, seed=7, marker=marker
    )
    artefact = make_tick_audio(
        62, SR, t0, {"BPM": 0.044}, snr_db=60.0, seed=99, marker=False
    )
    return wwv + artefact, label_timing(t0, walk_s, SR), t0


def test_a_second_1000_hz_peak_never_registers_as_bpm():
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    acq = RegistrationAcquirer("SHARED_10000", SR)
    audio, label, t0 = _two_train_minute(marker=True)
    reg = acq.offer_minute(audio, label, 1_000_000, MIN, D_SHARED, "ep-1")
    assert reg is not None, "the marker should have named WWV"
    assert reg.stations == ("WWV",)
    assert "BPM" not in reg.stations
    # the correction comes from the WWV peak alone: -walk, not a mean
    # pulled toward the artefact 34 ms away
    assert reg.sample0_utc_for(1_000_000) == pytest.approx(t0, abs=0.003)


def test_the_two_peak_pair_without_a_marker_stays_bootstrap():
    """Before task 16a this pair called itself unambiguous with support 2
    and anchored the station on it."""
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    acq = RegistrationAcquirer("SHARED_10000", SR)
    audio, label, _t0 = _two_train_minute(marker=False)
    assert acq.offer_minute(audio, label, 1_000_000, MIN, D_SHARED, "ep-1") is None
    assert acq.state == acq.STATE_BOOTSTRAP
    assert acq._open, "the WWV readings stay open"
    assert all(
        "BPM" not in {a[0] for a in h.assignments} for h in acq._open
    ), "no open hypothesis may name BPM"


# ── Task 17b: the whole-second correction, bounded and unwrapped ──────
#
# Review finding W2/C2 (task-15-16-review.md): ``marker_agrees`` and
# ``marker_names_one_hypothesis`` both tested
# ``abs(wrap_half_second(offset - predicted)) <= 10 ms``, and the wrap
# discards exactly the quantity ``integer_second_correction`` then acts
# on.  A marker landing a WHOLE SECOND from the fold position therefore
# "agreed", and the plane moved by a second.  Nothing downstream could
# see it: ticks are 1 s periodic and ``TickEdgeDetector`` searches
# ±20 ms, so a plane wrong by exactly ±1 s produces err ≈ 0, gate (b)
# passes, ``corroborate`` tightens, and the station publishes a silent
# 1 s UTC error as authoritative T3.
#
# The numbers below are B4's own measured values, quoted from the review.

D_WWV_B4 = 0.004027  # measured expected delay, s
FOLD_PEAK_B4 = 0.08079  # measured fold position, s
CORR_B4 = D_WWV_B4 - FOLD_PEAK_B4  # -76.763 ms


def _decide(marker_offset_s, snr_db=30.0, confirmed_k=None):
    from hf_timestd.core.registration_acquirer import whole_second_from_marker

    return whole_second_from_marker(
        marker_offset_s,
        snr_db,
        D_WWV_B4,
        CORR_B4,
        confirmed_k=confirmed_k,
    )


def test_the_predicted_marker_position_is_b4s_measured_fold_peak():
    """Anchors the fixture: the review's +80.790 ms."""
    from hf_timestd.core.registration_acquirer import marker_position_s

    assert marker_position_s(D_WWV_B4, CORR_B4) == pytest.approx(FOLD_PEAK_B4)


def test_a_marker_on_the_fold_peak_agrees_and_asks_for_no_whole_second():
    d = _decide(FOLD_PEAK_B4)
    assert d.agrees is True
    assert d.k_raw == 0
    assert d.k_int == 0
    assert d.unresolved is False


@pytest.mark.parametrize(
    "marker_offset_s,k_raw",
    [
        (FOLD_PEAK_B4 + 1.0, -1),  # the review's +1080.790 ms
        (FOLD_PEAK_B4 - 1.0, +1),  # the review's  -919.210 ms
    ],
)
def test_a_marker_a_whole_second_off_no_longer_agrees(marker_offset_s, k_raw):
    """W2, closed.  The comparison is unwrapped, so a second is a second.

    At 7a99e21 both of these returned ``agrees=True`` and shifted the
    plane by ∓1 s — the +1080.8 ms case moved it -1076.8 ms.
    """
    d = _decide(marker_offset_s)
    assert d.agrees is False
    assert d.k_raw == k_raw, "the whole second the marker implies is still measured"
    assert d.k_int == 0, "...and still refused"
    assert d.unresolved is True
    assert d.reason == "disagrees"


def test_a_marker_a_whole_second_off_is_refused_even_when_confirmed():
    """Agreement is a gate in its own right, not one vote among four."""
    d = _decide(FOLD_PEAK_B4 + 1.0, snr_db=40.0, confirmed_k=-1)
    assert d.k_int == 0 and d.unresolved is True


# The bounds on a whole second that DOES stand on the ticks.
#
# The unwrapped agreement test does most of the work by itself: agreement
# forces ``k_raw`` to equal the wrap ``marker_position_s`` itself applied,
# which is 0 unless ``expected_delay − correction`` fell outside
# (−0.5, 0.5].  So an AGREEING non-zero whole second is exactly the case
# the marker exists for -- a fold whose fractional correction wrapped and
# hid a second -- and it is narrow.  D = 12 ms with a fold correction of
# −0.49 s is such a case: the marker at −0.498 s says the label is 0.51 s
# out, the fold wrapped that to −0.49 s, and k = +1 recovers it.

D_WRAP = 0.012
FRAC_WRAP = -0.49
MARKER_WRAP = -0.498  # within 10 ms of marker_position_s(D_WRAP, FRAC_WRAP)


def _decide_wrapped(snr_db=30.0, confirmed_k=None, frac=FRAC_WRAP):
    from hf_timestd.core.registration_acquirer import whole_second_from_marker

    return whole_second_from_marker(
        MARKER_WRAP, snr_db, D_WRAP, frac, confirmed_k=confirmed_k
    )


def test_the_hidden_second_case_agrees_and_asks_for_one_second():
    d = _decide_wrapped()
    assert d.agrees is True
    assert d.k_raw == 1


def test_a_confirmed_one_second_correction_is_applied():
    d = _decide_wrapped(snr_db=25.0, confirmed_k=1)
    assert d.k_int == 1
    assert d.unresolved is False
    assert d.reason == "confirmed"


def test_an_unconfirmed_one_second_correction_waits():
    """One minute of evidence is not enough to move UTC by a second.

    The review measured the marker at +81.712 and +81.837 ms on
    consecutive minutes, so a second minute is evidence a healthy channel
    already has in hand.
    """
    d = _decide_wrapped(snr_db=25.0, confirmed_k=None)
    assert d.k_int == 0 and d.unresolved is True
    assert d.reason == "unconfirmed"


def test_a_different_k_on_the_previous_minute_does_not_confirm():
    d = _decide_wrapped(snr_db=25.0, confirmed_k=-1)
    assert d.k_int == 0 and d.reason == "unconfirmed"


def test_a_weak_marker_may_not_move_the_whole_second():
    """MARKER_MIN_SNR_DB = 6 dB promotes; 20 dB is needed to move UTC.

    The measured markers ran 26-39 dB, so the bar sits well below the
    evidence and well above the promotion floor (review I3).
    """
    from hf_timestd.core.registration_acquirer import (
        MARKER_INT_SECOND_MIN_SNR_DB,
    )

    assert MARKER_INT_SECOND_MIN_SNR_DB == 20.0
    d = _decide_wrapped(snr_db=19.9, confirmed_k=1)
    assert d.k_int == 0 and d.unresolved is True and d.reason == "snr"
    assert _decide_wrapped(snr_db=20.0, confirmed_k=1).k_int == 1


def test_more_than_one_second_is_never_applied():
    """|k_int| <= 1, as a belt on the agreement test's own algebra.

    Agreement can only ever imply |k| <= 1 while ``correction`` really is
    fractional.  This passes a correction that is not (−1.49 s, as an
    unwrapped caller would), and the magnitude bound catches the two
    seconds that follows.
    """
    d = _decide_wrapped(snr_db=30.0, confirmed_k=2, frac=-1.49)
    assert d.agrees is True
    assert d.k_raw == 2
    assert d.k_int == 0 and d.unresolved is True and d.reason == "magnitude"


def test_a_nan_snr_never_passes_the_whole_second_gate():
    """M1: ``nan < 6.0`` is False, so a NaN SNR passes the promotion
    floor.  It must not also pass this one."""
    d = _decide_wrapped(snr_db=float("nan"), confirmed_k=1)
    assert d.k_int == 0 and d.reason == "snr"


# ── the acquirer, end to end ─────────────────────────────────────────


def test_the_acquirer_publishes_whole_second_unresolved():
    """A registration whose whole second was refused says so.

    Not a refusal: the plane is the fold plane, whose whole second comes
    from the label frame exactly as it did before the marker search was
    reachable at all (c7b2106 pinned k_int at 0 on every live minute).
    The flag is provenance — "this plane's second rests on the host's
    frame, not on the marker" — and it reaches the channel file so the
    offline analysis and the operator can both see it.
    """
    from hf_timestd.core.registration_acquirer import Registration

    r = Registration(
        counter_epoch_id="ep-1",
        rtp_ref=0,
        utc_ref=0.0,
        sample_rate=SR,
        sigma_ms=1.0,
    )
    assert r.whole_second_unresolved is False


def test_the_flag_round_trips_through_the_channel_file(tmp_path):
    from hf_timestd.core.registration_acquirer import Registration
    from hf_timestd.core.registration_store import RegistrationStore

    store = RegistrationStore(
        directory=tmp_path / "reg",
        summary_path=tmp_path / "registration.json",
        time_fn=lambda: 1_800_000_000.0,
    )
    reg = Registration(
        counter_epoch_id="ep-1",
        rtp_ref=1_000_000,
        utc_ref=1_800_000_000.0,
        sample_rate=SR,
        sigma_ms=1.0,
        channel="WWV_10000",
        n_minutes=5,
        verified=True,
        whole_second_unresolved=True,
    )
    store.write_channel(reg, "ACQUIRED", {})
    import json

    payload = json.loads((tmp_path / "reg" / "WWV_10000.json").read_text())
    assert payload["whole_second_unresolved"] is True
    back = store.read_siblings()
    assert len(back) == 1 and back[0].whole_second_unresolved is True


def test_the_fused_plane_inherits_an_unresolved_second(tmp_path):
    """``any``: one member whose second rests on the host's frame makes
    the fused plane's second rest on it too."""
    from hf_timestd.core.registration_acquirer import Registration
    from hf_timestd.core.registration_store import fuse_registrations
    from synth_origins import counter_map_for

    def _r(channel, unresolved):
        return Registration(
            counter_epoch_id="ep-1",
            rtp_ref=1_000_000,
            utc_ref=1_800_000_000.0,
            sample_rate=SR,
            sigma_ms=1.0,
            channel=channel,
            n_minutes=5,
            verified=True,
            epoch_offset_s=0.0,
            whole_second_unresolved=unresolved,
            counter_map=m51,
        )

    # Task 6: each channel starts its own RTP counter; state a shared
    # origin (start block 51) so the two channels can combine.
    m51 = counter_map_for(51, SR)
    clean = fuse_registrations([_r("a", False), _r("b", False)], 1_000_000, m51, "a")
    assert clean.whole_second_unresolved is False
    mixed = fuse_registrations([_r("a", False), _r("b", True)], 1_000_000, m51, "a")
    assert mixed.whole_second_unresolved is True


def test_a_marker_one_second_off_no_longer_moves_the_acquired_plane(monkeypatch):
    """The W2 regression, end to end through ``_try_acquire``.

    The real marker sits at d + walk = +110 ms, exactly where the fold
    puts WWV's ticks.  Displace the SEARCH result by a whole second — a
    sidelobe of the 800 ms tone a second away, a neighbouring minute's
    marker reached by the ±1.5 s window — and at 7a99e21 the plane moved
    -1 s and the station published it as authoritative T3 inside three
    minutes.  Now the plane lands on truth and says its second is
    unresolved.
    """
    from hf_timestd.core import registration_acquirer as ra

    real = ra.marker_in_envelope

    def displaced(env, sample_rate, sample0_utc_label, minute_utc):
        got = real(env, sample_rate, sample0_utc_label, minute_utc)
        return None if got is None else (got[0] + 1.0, 30.0)

    monkeypatch.setattr(ra, "marker_in_envelope", displaced)

    acq = ra.RegistrationAcquirer("SHARED_10000", SR)
    audio, label, t0 = _shared_channel_minute(walk_s=0.100, marker=True)
    reg = acq.offer_minute(audio, label, 1_000_000, MIN, D_SHARED, "ep-1")
    assert reg is not None, "the marker still NAMES the station"
    assert reg.stations == ("WWV",)
    # The plane is the fold plane: on truth, not a second away from it.
    assert reg.sample0_utc_for(1_000_000) == pytest.approx(t0, abs=0.003)
    assert reg.whole_second_unresolved is True


def test_a_marker_on_the_ticks_leaves_the_second_resolved():
    """The same audio, undisplaced: nothing to resolve, nothing flagged."""
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    acq = RegistrationAcquirer("SHARED_10000", SR)
    audio, label, t0 = _shared_channel_minute(walk_s=0.100, marker=True)
    reg = acq.offer_minute(audio, label, 1_000_000, MIN, D_SHARED, "ep-1")
    assert reg is not None
    assert reg.whole_second_unresolved is False
    assert reg.sample0_utc_for(1_000_000) == pytest.approx(t0, abs=0.003)


def test_the_hold_state_clears_on_reset():
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    acq = RegistrationAcquirer("SHARED_10000", SR)
    acq._marker_k_seen = (MIN, -1)
    acq._marker_k_holds = 2
    acq.reset("test")
    assert acq._marker_k_seen is None and acq._marker_k_holds == 0


# ── Task 17 review I1: a disagreeing marker is never silent ───────────
#
# The silent window lives on a DEDICATED channel, where the fold names
# the station on its own (one eligible station in the band, so the
# hypothesis is unambiguous) and the marker is consulted for the whole
# second alone.  On a shared channel the marker also has to NAME the
# station, and that test refuses a displaced marker before the
# whole-second code is reached at all -- which is why the window the
# review measured is a dedicated-channel window.  B4, where the review
# took its numbers, runs dedicated WWV channels.

D_DEDICATED = {"WWV": 0.010}


def _disagreement_log(monkeypatch, caplog, displace_s):
    """Acquire one minute on a DEDICATED channel with the marker search
    result displaced by ``displace_s``, and return what was logged."""
    import logging

    from hf_timestd.core import registration_acquirer as ra

    real = ra.marker_in_envelope

    def displaced(env, sample_rate, sample0_utc_label, minute_utc):
        got = real(env, sample_rate, sample0_utc_label, minute_utc)
        return None if got is None else (got[0] + displace_s, 30.0)

    monkeypatch.setattr(ra, "marker_in_envelope", displaced)
    acq = ra.RegistrationAcquirer("WWV_10000", SR)
    audio, label, t0 = _shared_channel_minute(walk_s=0.100, marker=True)
    with caplog.at_level(logging.INFO, logger="hf_timestd.core.registration_acquirer"):
        reg = acq.offer_minute(audio, label, 1_000_000, MIN, D_DEDICATED, "ep-1")
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    return reg, warnings, t0


@pytest.mark.parametrize("displace_s", [0.030, 0.200, 0.400, 0.520, 1.0])
def test_a_disagreeing_marker_always_says_so(monkeypatch, caplog, displace_s):
    """I1.  Logged on disagreement alone, whatever whole second it implies.

    17b routed the log through ``dec.reason``, and
    ``whole_second_from_marker`` answers ``"zero"`` and returns before it
    consults ``agrees`` — so every displacement implying no whole second
    (roughly −418 ms to +580 ms of the predicted position, nearly the
    whole search window) passed in SILENCE.  The first four cases here
    are the review's own probe points, three of which said nothing.

    That window holds review C3's class (ii): the ~37 ms WWV-vs-BPM
    misnaming, the remaining live risk, which had just lost its only
    witness.  The 1.0 s case is the W2 marker, which DOES imply a second
    — it must log through the same branch, so one fault reads as one
    message whatever its magnitude.
    """
    reg, warnings, t0 = _disagreement_log(monkeypatch, caplog, displace_s)
    assert reg is not None, "the fold names the station without the marker"
    disagreements = [w for w in warnings if "disagrees with hypothesis" in w]
    assert len(disagreements) == 1, warnings
    line = disagreements[0]
    assert "disagrees with hypothesis WWV" in line
    assert "predicted" in line
    assert "SNR 30.0 dB" in line
    # ...and the plane is still the fold plane, on truth.
    assert reg.sample0_utc_for(1_000_000) == pytest.approx(t0, abs=0.003)


def test_an_agreeing_marker_logs_no_disagreement(monkeypatch, caplog):
    """The control: the marker on the ticks must stay quiet."""
    reg, warnings, _t0 = _disagreement_log(monkeypatch, caplog, 0.0)
    assert reg is not None
    assert reg.whole_second_unresolved is False
    assert not [w for w in warnings if "disagrees with hypothesis" in w]


def test_the_disagreement_log_does_not_need_an_unresolved_second(monkeypatch, caplog):
    """The distinction I1 turns on: disagreement and an unresolved whole
    second are different facts, and the log reports the first even when
    the second is absent."""
    reg, warnings, _t0 = _disagreement_log(monkeypatch, caplog, 0.200)
    assert reg.whole_second_unresolved is False, "no whole second implied"
    assert [w for w in warnings if "disagrees with hypothesis" in w]
