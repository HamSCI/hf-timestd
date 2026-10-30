"""Spec §8 replay acceptance on real station chunks copied to the devbox.

Runs ONLY on the devbox: the fixtures live outside the repo
(``/home/mjh/hamsci/fixtures/...``) and every test here skips when its
fixture is absent.  Never run this module on a station -- the ND fold
sweep that produced these numbers loaded two station recorders to their
watchdog on 2026-09-06 (see the task-11 brief).

Fixtures (devbox only):
  - ``nd-20260906/1788729000.{bin.zst,json}``      ND good window (21:20Z)
  - ``nd-20260906-bad/1788696000.{bin.zst,json}``  ND dark window (12:00Z)
  - ``b4-20260907/1788742800.{bin.zst,json}``       B4 with T6 (01:00Z)
  - ``nd-20260906/wwv_20000-1788729000.{bin.zst,json}``  ND same-site
    sibling, WWV_20000, identical window to the good-window chunk
  - ``nd-20260906/wwv_25000-1788729000.{bin.zst,json}``  ND same-site
    sibling, WWV_25000, identical window

``MetrologyEngine``'s real constructor takes ``raw_buffer_dir``,
``output_dir``, ``channel_name``, ``frequency_hz``, ``receiver_grid``,
``sample_rate``, ``precise_lat``, ``precise_lon`` (see
``metrology_engine.py`` ~L234 and how ``metrology_service.py`` ~L203-220
builds one) -- NOT the four-argument constructor the original brief
guessed.  ``raw_buffer_dir`` is a legacy no-op the engine only stores;
``output_dir`` needs to exist on disk but nothing here writes physics
products (``enable_physics_products=False``: timing-only, skip the
secondary-arrival search).  The sidecar's ``station`` block already
carries ``grid_square`` directly, so no lat/lon -> grid conversion is
needed.
"""

import dataclasses
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

FIX = Path(
    os.environ.get("HF_TIMESTD_ND_FIXTURE", "/home/mjh/hamsci/fixtures/nd-20260906")
)
CHUNK = FIX / "1788729000.bin.zst"
SIDECAR = FIX / "1788729000.json"

pytestmark = pytest.mark.skipif(not CHUNK.exists(), reason="ND fixture not present")


def _engine(meta: dict):
    """Build the engine the way ``metrology_service.py`` does (task-11
    controller ruling): ``raw_buffer_dir`` is a legacy no-op argument,
    ``output_dir`` gets a throwaway temp directory, ``receiver_grid``
    comes straight from the sidecar's ``station.grid_square`` (present
    on all three fixtures -- no maidenhead conversion needed), and
    ``precise_lat``/``precise_lon`` drive the actual geometric-delay
    math that ``expected_delays_s`` uses."""
    from hf_timestd.core.metrology_engine import MetrologyEngine

    st = meta["station"]
    out_dir = Path(tempfile.mkdtemp(prefix="hf_timestd_replay_"))
    return MetrologyEngine(
        raw_buffer_dir=Path("/dev/null"),
        output_dir=out_dir,
        channel_name=meta["channel_name"],
        frequency_hz=float(meta["frequency_hz"]),
        receiver_grid=st.get("grid_square", ""),
        sample_rate=int(meta["sample_rate"]),
        precise_lat=float(st["latitude"]),
        precise_lon=float(st["longitude"]),
        enable_physics_products=False,
    )


@pytest.fixture(scope="module")
def chunk():
    import zstandard

    raw = zstandard.ZstdDecompressor().decompress(
        CHUNK.read_bytes(), max_output_size=1 << 31
    )
    iq = np.frombuffer(raw, dtype="<c8")
    meta = json.loads(SIDECAR.read_text())
    return iq, meta


@pytest.mark.parametrize("shift_ms", [-300.0, -100.0, 50.0, 250.0])
def test_acquisition_recovers_a_shifted_label(chunk, shift_ms):
    from hf_timestd.core.buffer_timing import resolve_buffer_timing
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer
    from hf_timestd.core.tick_edge_detector import TickEdgeDetector

    iq, meta = chunk
    sr = int(meta["sample_rate"])
    eng = _engine(meta)
    acq = RegistrationAcquirer("SHARED_10000", sr)
    bt_true = resolve_buffer_timing(meta, sample_rate=sr)
    got = None
    a = 0
    for k in range(3):  # up to 180 s of bootstrap
        a = k * 60 * sr
        seg = iq[a : a + 62 * sr]
        minute_utc = int(meta["minute_boundary"]) + 60 * k
        label = dataclasses.replace(
            bt_true, sample0_utc=bt_true.sample0_utc + a / sr + shift_ms / 1000.0
        )
        audio = eng.prepare_audio(seg)
        delays = eng.expected_delays_s(label.sample0_utc, minute_utc)
        got = acq.offer_minute(
            audio,
            label,
            int(meta["start_rtp_timestamp"]) + a,
            minute_utc,
            delays,
            "ep-nd",
        )
        if got is not None:
            break
    assert got is not None, f"no acquisition within 180 s (shift {shift_ms:+.1f} ms)"
    recovered_ms = (
        got.sample0_utc_for(int(meta["start_rtp_timestamp"]) + a)
        - (bt_true.sample0_utc + a / sr)
    ) * 1000.0
    # fine search on the acquired plane
    bt_acq = dataclasses.replace(
        bt_true,
        sample0_utc=got.sample0_utc_for(int(meta["start_rtp_timestamp"]) + a),
        origin_source="acquired",
        origin_sigma_ms=got.sigma_ms,
    )
    det = TickEdgeDetector(sample_rate=sr)
    res = det.detect_edges(
        audio_signal=audio,
        station="WWV",
        minute_number=minute_utc,
        buffer_timing=bt_acq,
        expected_delay_sec=delays["WWV"],
        is_dedicated_channel=False,
        iq_samples=None,
    )
    print(
        f"\n  shift_ms={shift_ms:+7.1f}  minutes={k + 1}  recovered_ms={recovered_ms:+8.3f}  "
        f"sigma_ms={got.sigma_ms:6.3f}  fine_sigma1={res.sigma_single_ms if res else float('nan'):7.3f}  "
        f"fine_err_ms={res.ensemble_timing_error_ms if res else float('nan'):+7.3f}  "
        f"n_detected={res.n_detected if res else -1}"
    )
    # Truth reference (task-11 controller ruling): the sidecar plane on this
    # fixture was measured 2026-09-06 to sit ~+8.8 ms from where the WWV
    # ticks actually are, so "within truth" means within ±2 ms of that WWV
    # timing error AFTER the fine search's own residual is folded in.
    assert res is not None and res.anchor_source == "acquired"
    assert abs(res.ensemble_timing_error_ms) < 2.0, (
        f"fine search on the acquired plane still shows {res.ensemble_timing_error_ms:+.2f} ms "
        f"of error -- the acquired plane is not converging to the ticks"
    )
    assert (
        res.sigma_single_ms < 1.0
    ), f"fine sigma1 {res.sigma_single_ms:.2f} ms >= 1.0 ms"
    assert (
        res.n_detected >= 40
    ), f"only {res.n_detected} ticks detected on the acquired plane"


BAD = Path(
    os.environ.get(
        "HF_TIMESTD_ND_BAD_FIXTURE", "/home/mjh/hamsci/fixtures/nd-20260906-bad"
    )
)
B4 = Path(
    os.environ.get("HF_TIMESTD_B4_FIXTURE", "/home/mjh/hamsci/fixtures/b4-20260907")
)
# Real same-site sibling: B4's WWV_20000 channel, same radiod instance and
# RTP counter as the SHARED_10000 chunk above, same 10-minute window
# (task-11 fix round 2, night: 2026-09-07 01:00Z).  Confirmed same counter
# space: the two sidecars' own (gps_time_ns, rtp_timesnap) pairs,
# extrapolated to a common RTP via resolve_buffer_timing, agree to
# ~1.3 ms -- well inside "a few ms".
B4_DAY = Path(
    os.environ.get(
        "HF_TIMESTD_B4_DAY_FIXTURE", "/home/mjh/hamsci/fixtures/b4-20260906-day"
    )
)
# Daytime B4 pair (task-11 fix round 3): 2026-09-06 18:00-18:05Z, same
# radiod/RTP-counter relationship as B4_DAY, confirmed via
# hf_timestd.core.cross_channel_rtp.same_counter_space (agrees to
# ~1.1 ms).  Different filename convention from the night pair
# ("shared_10000-..." / "wwv_20000-..." with underscores, vs.
# "1788742800" / "wwv20000-1788742800" for the night pair).


def _load(dirpath: Path, stem: str):
    import zstandard

    raw = zstandard.ZstdDecompressor().decompress(
        (dirpath / f"{stem}.bin.zst").read_bytes(), max_output_size=1 << 31
    )
    return np.frombuffer(raw, dtype="<c8"), json.loads(
        (dirpath / f"{stem}.json").read_text()
    )


def _acquire_first_minutes(iq, meta, shift_ms=0.0):
    """Run the acquirer over the first <=3 minutes of a chunk with the
    sidecar label shifted.  Returns (registration, start_rtp_of_last_minute,
    audio, delays, minute_utc, bt_true, offset_samples)."""
    from hf_timestd.core.buffer_timing import resolve_buffer_timing
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    sr = int(meta["sample_rate"])
    eng = _engine(meta)
    acq = RegistrationAcquirer(meta["channel_name"], sr)
    bt_true = resolve_buffer_timing(meta, sample_rate=sr)
    got, a = None, 0
    for k in range(3):
        a = k * 60 * sr
        seg = iq[a : a + 62 * sr]
        minute_utc = int(meta["minute_boundary"]) + 60 * k
        label = dataclasses.replace(
            bt_true, sample0_utc=bt_true.sample0_utc + a / sr + shift_ms / 1000.0
        )
        audio = eng.prepare_audio(seg)
        delays = eng.expected_delays_s(label.sample0_utc, minute_utc)
        got = acq.offer_minute(
            audio,
            label,
            int(meta["start_rtp_timestamp"]) + a,
            minute_utc,
            delays,
            "ep-replay",
        )
        if got is not None:
            break
    return (
        got,
        int(meta["start_rtp_timestamp"]) + a,
        audio,
        delays,
        minute_utc,
        bt_true,
        a,
    )


@pytest.mark.skipif(
    not (BAD / "1788696000.bin.zst").exists(),
    reason="ND bad-window fixture not present",
)
def test_bad_window_chunk_goes_from_junk_to_ticks_without_restart():
    """Spec §8: an ND chunk from the dark window (09-06 12:00Z) must
    acquire.  Whatever the sidecar plane says, acquisition must land a
    plane on which the fine search sees ticks.

    Fix-round-1 finding: on this chunk the acquirer correctly locks onto
    WWVH (`stations=('WWVH',)`) -- WWV's Fort Collins path isn't open to
    ND at 12:00Z while WWVH's Kauai path is.  The 1000-band peak this
    fold sees is WWVH's own tick LEAKING into the WWV/BPM band
    (`arbitrate_bands` correctly discards it), not a real WWV tick, so a
    fine search hard-coded to `station="WWV"` reads ~the WWV-WWVH delay
    separation (18.7 ms predicted here) as spurious "error" against a
    plane that was never claiming to be WWV.  Query the station the
    acquirer actually locked (`got.stations[0]`) instead."""
    iq, meta = _load(BAD, "1788696000")
    sr = int(meta["sample_rate"])
    got, rtp, audio, delays, minute_utc, bt_true, a = _acquire_first_minutes(
        iq, meta, shift_ms=-250.0
    )
    assert got is not None, "no acquisition within 180 s on the bad-window chunk"
    from hf_timestd.core.tick_edge_detector import TickEdgeDetector

    station = got.stations[0]
    bt_acq = dataclasses.replace(
        bt_true,
        sample0_utc=got.sample0_utc_for(rtp),
        origin_source="acquired",
        origin_sigma_ms=got.sigma_ms,
    )
    det = TickEdgeDetector(sample_rate=sr)
    res = det.detect_edges(
        audio_signal=audio,
        station=station,
        minute_number=minute_utc,
        buffer_timing=bt_acq,
        expected_delay_sec=delays[station],
        is_dedicated_channel=False,
        iq_samples=None,
    )
    print(
        f"\n  bad-window: acquired stations={got.stations}  station queried={station}  "
        f"sigma1={res.sigma_single_ms if res else float('nan'):.3f} ms  "
        f"n_detected={res.n_detected if res else -1}  "
        f"fine_err_ms={res.ensemble_timing_error_ms if res else float('nan'):+.3f}"
    )
    assert res is not None
    assert res.sigma_single_ms < 1.5, f"sigma1 {res.sigma_single_ms:.2f} ms >= 1.5 ms"
    assert res.n_detected >= 30, f"only {res.n_detected} ticks detected"
    assert abs(res.ensemble_timing_error_ms) < 2.0, (
        f"fine search on the acquired plane for its own station ({station}) still "
        f"shows {res.ensemble_timing_error_ms:+.2f} ms of error"
    )


def _acquire_channel(iq, meta, shift_ms, epoch_id, engine=None, max_k=3):
    """Run the acquirer over up to ``max_k`` minutes of one channel's
    chunk, 60 s apart, each a 62 s buffer (matches ``offer_minute``'s own
    62 s-per-minute convention).  Returns (registration_or_None, acquirer,
    start_rtp_of_last_minute, audio, delays, minute_utc, bt_true,
    offset_samples, k)."""
    from hf_timestd.core.buffer_timing import resolve_buffer_timing
    from hf_timestd.core.registration_acquirer import RegistrationAcquirer

    sr = int(meta["sample_rate"])
    eng = engine or _engine(meta)
    acq = RegistrationAcquirer(meta["channel_name"], sr)
    bt_true = resolve_buffer_timing(meta, sample_rate=sr)
    got, a, k = None, 0, 0
    audio, delays, minute_utc = None, None, None
    for k in range(max_k):
        a = k * 60 * sr
        seg = iq[a : a + 62 * sr]
        minute_utc = int(meta["minute_boundary"]) + 60 * k
        label = dataclasses.replace(
            bt_true, sample0_utc=bt_true.sample0_utc + a / sr + shift_ms / 1000.0
        )
        audio = eng.prepare_audio(seg)
        delays = eng.expected_delays_s(label.sample0_utc, minute_utc)
        start_rtp = int(meta["start_rtp_timestamp"]) + a
        got = acq.offer_minute(audio, label, start_rtp, minute_utc, delays, epoch_id)
        if got is not None:
            break
    return (
        got,
        acq,
        int(meta["start_rtp_timestamp"]) + a,
        audio,
        delays,
        minute_utc,
        bt_true,
        a,
        k,
    )


def _max_fold_snr_by_band(iq, meta, sr):
    """Diagnostic-only: the best fold-peak SNR reachable in each tone band
    using ALL available minutes (not the acquirer's 3-minute bootstrap
    cap) -- answers "is there any usable tick signal in this chunk at
    all," independent of window-length or label-shift choices."""
    from hf_timestd.core import registration_acquirer as ra
    from hf_timestd.core.buffer_timing import resolve_buffer_timing

    bt_true = resolve_buffer_timing(meta, sample_rate=sr)
    eng = _engine(meta)
    audio_all = eng.prepare_audio(iq)
    n_sec = min(270, len(audio_all) // sr)
    out = {}
    for band in ra.TONE_BANDS_HZ:
        profile, rows = ra.fold_tick_train(
            audio_all, sr, bt_true.sample0_utc, band, n_sec
        )
        if rows == 0:
            out[band] = (0, float("nan"))
            continue
        baseline = np.median(profile)
        dev = profile - baseline
        mad = np.median(np.abs(dev)) * 1.4826
        snr_db = 20 * np.log10(dev.max() / mad) if mad > 0 else float("nan")
        out[band] = (rows, snr_db)
    return out


@pytest.mark.skipif(
    not (B4 / "wwv20000-1788742800.bin.zst").exists(),
    reason="B4 night-window WWV_20000 fixture not present",
)
def test_b4_night_window_wwv20000_has_no_acquirable_tick():
    """Documents a real, negative finding (task-11 fix rounds 2-3), not a
    bug: B4's WWV_20000 channel (20 MHz) carries no acquirable tick at
    2026-09-07 01:00Z.  The fold-peak SNR in both tone bands never
    crosses the ~13 dB detection floor even integrating every available
    minute of the 5-minute chunk, and the raw IQ amplitude is ~35 dB
    below the SHARED_10000 (10 MHz) chunk from the identical window --
    checked three ways in the fix-round-2 report (amplitude, PSD, file
    integrity) to rule out a decode bug.  The acquirer's response is
    correct: it stays BOOTSTRAP rather than manufacturing a registration
    from noise.  No xfail here -- this is the expected, documented
    behaviour of a dark channel, asserted directly."""
    iq, meta = _load(B4, "wwv20000-1788742800")
    sr = int(meta["sample_rate"])
    got, acq, rtp, audio, delays, minute_utc, bt_true, a, k = _acquire_channel(
        iq, meta, shift_ms=0.0, epoch_id="ep-b4-night"
    )
    snr_by_band = _max_fold_snr_by_band(iq, meta, sr)
    print(
        f"\n  WWV_20000 (night, 01:00Z): {k + 1} minutes offered, state={acq.state}, "
        f"got={'ACQUIRED' if got is not None else None}"
    )
    print(
        "  WWV_20000 max fold SNR over all available minutes "
        f"({'/'.join(f'{b}:{s:.1f}dB(n={n})' for b, (n, s) in snr_by_band.items())})"
    )
    assert got is None, "WWV_20000 should NOT acquire in this dark window"
    assert acq.state == acq.STATE_BOOTSTRAP


@pytest.mark.skipif(
    not (B4 / "1788742800.bin.zst").exists(),
    reason="B4 night-window SHARED_10000 fixture not present",
)
def test_b4_night_shared_channel_named_by_its_minute_marker():
    """Task 15 acceptance: B4's SHARED_10000 chunk (2026-09-07 01:00Z)
    folds ONE 1000 Hz peak, so ``fit_template`` leaves {WWV, BPM} open 37
    ms apart, and B4's dedicated WWV_20000 channel is dark in this window
    (the test above) -- no sibling can choose.  This was the designed
    BOOTSTRAP negative through task-11b.

    The 800 ms minute marker resolves it without a sibling: WWV transmits
    a 1000 Hz tone at second 0 and BPM transmits no minute marker, so an
    800 ms tone standing on the same fold position as the ticks names WWV
    and excludes BPM (spec §12).  The marker only becomes searchable once
    a SECOND minute is buffered -- the +-1.5 s search needs run-up ahead
    of the minute, and the live ring hands the acquirer [minute, minute +
    60 s) -- so this asserts acquisition within the acquirer's 3-minute
    bootstrap, not within one minute.

    Acceptance: acquires ``('WWV',)``, and the fine search on the
    promoted plane sees a real tick train (sigma_1 < 1 ms, n >= 40)."""
    from hf_timestd.core import registration_acquirer as ra
    from hf_timestd.core.buffer_timing import resolve_buffer_timing
    from hf_timestd.core.tick_edge_detector import TickEdgeDetector

    iq, meta = _load(B4, "1788742800")
    sr = int(meta["sample_rate"])
    got, acq, rtp, audio, delays, minute_utc, bt_true, a, k = _acquire_channel(
        iq, meta, shift_ms=0.0, epoch_id="ep-b4-night-shared"
    )
    print(
        f"\n  B4 SHARED_10000 (night, 01:00Z): {k + 1} minutes offered, "
        f"state={acq.state}, got="
        f"{'ACQUIRED' + str(got.stations) if got is not None else None}"
    )
    print(
        "  expected delays (ms): "
        + ", ".join(f"{s}={d * 1000:.3f}" for s, d in sorted(delays.items()))
    )

    # The marker as an independent measurement, in the same frame
    # ``_try_acquire`` searches (the concatenated buffer, oldest label):
    # report its offset and SNR whether or not the acquirer used it.
    eng = _engine(meta)
    audio_all = eng.prepare_audio(iq[: 182 * sr])
    s0 = resolve_buffer_timing(meta, sample_rate=sr).sample0_utc
    marker_snr = {}
    for band in ra.TONE_BANDS_HZ:
        for j in range(3):
            mk = ra.locate_minute_marker(
                audio_all, sr, s0, band, int(meta["minute_boundary"]) + 60 * j
            )
            print(
                f"  marker band {band} minute +{j}: "
                + (
                    "None"
                    if mk is None
                    else f"offset={mk[0] * 1000:+.3f} ms  snr={mk[1]:.2f} dB"
                )
            )
            if mk is not None:
                marker_snr.setdefault(band, mk)
    assert "1000" in marker_snr, (
        "no 800 ms marker found in the 1000 band on this chunk: NEEDS_CONTEXT "
        f"(marker search results above, fold SNR {_max_fold_snr_by_band(iq, meta, sr)})"
    )

    assert got is not None, (
        f"no acquisition within {k + 1} minutes (state={acq.state}, "
        f"open={len(acq._open)}) -- the marker should have named WWV"
    )
    assert got.stations == ("WWV",), f"marker named {got.stations}, not ('WWV',)"
    assert got.hypotheses_open == 0
    assert "marker" in got.method, f"method={got.method}"

    # ── the fine search on the promoted plane ──
    bt_acq = dataclasses.replace(
        bt_true,
        sample0_utc=got.sample0_utc_for(rtp),
        origin_source="acquired",
        origin_sigma_ms=got.sigma_ms,
    )
    res = TickEdgeDetector(sample_rate=sr).detect_edges(
        audio_signal=audio,
        station="WWV",
        minute_number=minute_utc,
        buffer_timing=bt_acq,
        expected_delay_sec=delays["WWV"],
        is_dedicated_channel=False,
        iq_samples=None,
    )
    print(
        f"  promoted plane: correction vs labelled plane = "
        f"{(got.sample0_utc_for(rtp) - (bt_true.sample0_utc + a / sr)) * 1000.0:+.3f} ms  "
        f"sigma={got.sigma_ms:.3f} ms"
    )
    print(
        f"  own-plane fine search WWV: "
        f"sigma1={res.sigma_single_ms if res else float('nan'):.3f} ms  "
        f"n={res.n_detected if res else -1}  "
        f"err={res.ensemble_timing_error_ms if res else float('nan'):+.3f} ms"
    )
    assert res is not None
    assert res.sigma_single_ms < 1.0, f"sigma1 {res.sigma_single_ms:.3f} ms >= 1 ms"
    assert res.n_detected >= 40, f"only {res.n_detected} ticks detected"


@pytest.mark.skipif(
    not (B4_DAY / "shared_10000-1788717600.bin.zst").exists()
    or not (B4_DAY / "wwv_20000-1788717600.bin.zst").exists(),
    reason="B4 daytime SHARED_10000/WWV_20000 pair not present",
)
def test_b4_shared_channel_resolved_by_real_wwv20000_sibling():
    """Spec §4: on a shared channel (SHARED_10000, WWV and BPM both keying
    the 1000 Hz tick band), standalone single-peak acquisition is NOT
    expected -- the design carries the open hypotheses and lets a sibling
    channel decide.  On B4 the dedicated WWV_20000/25000 channels do that
    live.  The night-window WWV_20000 chunk (see the test above) was dark;
    this daytime pair (2026-09-06 18:00-18:05Z, same radiod/RTP counter --
    confirmed via ``cross_channel_rtp.same_counter_space``, ~1.1 ms
    agreement) was supplied so the sibling path could run on real signal
    instead of a synthesized sibling.

    Fix-round-3 finding: WWV_20000 is ALSO dark in this daytime window
    (fold SNR 10.2/12.0 dB across the two tone bands, still under the
    ~13 dB floor) -- and its raw amplitude (RMS 2.15e-5) is essentially
    UNCHANGED from the night chunk's (2.22e-5), while SHARED_10000's
    amplitude swings 3x between the two chunks (0.00037 day vs. 0.00113
    night) as real HF propagation should.  A channel whose amplitude
    stays pinned at the same low floor regardless of time-of-day, while
    its sibling channel's amplitude moves with real propagation, is not
    behaving like a channel tracking an antenna -- this looks like a
    receive-chain issue specific to WWV_20000 at B4 (antenna, filter, or
    gain), not a day/night propagation effect, and is flagged here for
    Michael rather than silently worked around.

    SHARED_10000 in this window does NOT stay ambiguous either: it
    acquires UNAMBIGUOUSLY on its own as WWVH (not the {WWV, BPM} case),
    which the controller's ruling anticipated as a valid outcome ("or the
    WWVH band, that is fine too").  Both branches after that point --
    resolve_ambiguity against a {WWV, BPM} ambiguity, or fuse_registrations
    against an already-unambiguous SHARED plane -- need WWV_20000's own
    Registration, which this fixture cannot produce, so this test runs
    Step 1, prints the full diagnosis, and calls ``pytest.skip`` (never
    xfail) when it is not met -- both later branches are implemented and
    exercised whenever a fixture supplies a real WWV_20000 registration."""
    from hf_timestd.core.registration_acquirer import (
        CROSS_SITE_AGREE_MS,
        RegistrationAcquirer,
        SAME_SITE_AGREE_MS,
        wrap_half_second,
    )
    from hf_timestd.core.registration_store import fuse_registrations
    from hf_timestd.core.tick_edge_detector import TickEdgeDetector

    iq20, meta20 = _load(B4_DAY, "wwv_20000-1788717600")
    sr = int(meta20["sample_rate"])

    # ── Step 1: acquire WWV_20000 standalone (single-station channel) ──
    got20, acq20, rtp20, audio20, delays20, minute_utc20, bt_true20, a20, k20 = (
        _acquire_channel(iq20, meta20, shift_ms=0.0, epoch_id="ep-b4day", max_k=4)
    )
    snr_by_band = _max_fold_snr_by_band(iq20, meta20, sr)
    print(
        f"\n  WWV_20000 (day, 18:00Z) standalone: {k20 + 1} minutes offered, "
        f"state={acq20.state}, got={'ACQUIRED' if got20 is not None else None}"
    )
    print(
        "  WWV_20000 max fold SNR over all available minutes "
        f"({'/'.join(f'{b}:{s:.1f}dB(n={n})' for b, (n, s) in snr_by_band.items())})"
    )

    if got20 is None:
        pytest.skip(
            "WWV_20000 did not acquire standalone on this daytime fixture either: "
            f"no tone-band fold peak crosses the detection floor ({snr_by_band}); "
            "see the docstring's amplitude comparison -- this looks like a "
            "receive-chain issue specific to WWV_20000 at B4, not a day/night "
            "effect and not a bug in the acquirer.  The resolve_ambiguity/"
            "fuse_registrations paths below are implemented but need a fixture "
            "where WWV_20000 actually acquires to run."
        )

    # ── WWV_20000's own fine search on its own acquired plane ──
    bt_acq20 = dataclasses.replace(
        bt_true20,
        sample0_utc=got20.sample0_utc_for(rtp20),
        origin_source="acquired",
        origin_sigma_ms=got20.sigma_ms,
    )
    det = TickEdgeDetector(sample_rate=sr)
    res20 = det.detect_edges(
        audio_signal=audio20,
        station="WWV",
        minute_number=minute_utc20,
        buffer_timing=bt_acq20,
        expected_delay_sec=delays20["WWV"],
        is_dedicated_channel=True,
        iq_samples=None,
    )
    print(
        f"  WWV_20000 own-plane fine search: sigma={got20.sigma_ms:.3f} ms  "
        f"sigma1={res20.sigma_single_ms if res20 else float('nan'):.3f} ms  "
        f"n={res20.n_detected if res20 else -1}  "
        f"err={res20.ensemble_timing_error_ms if res20 else float('nan'):+.3f} ms"
    )
    assert got20.stations == ("WWV",)

    # ── Step 2: SHARED_10000 -- may bootstrap ambiguous, or acquire on its
    # own (the controller's ruling treats either as a valid daytime outcome)
    iq_shared, meta_shared = _load(B4_DAY, "shared_10000-1788717600")
    gotS, acqS, rtpS, audioS, delaysS, minute_utcS, bt_trueS, aS, kS = _acquire_channel(
        iq_shared, meta_shared, shift_ms=120.0, epoch_id="ep-b4day", max_k=4
    )
    print(
        f"  SHARED_10000 (day) after {kS + 1} minutes: state={acqS.state}, "
        f"got={'ACQUIRED(' + str(gotS.stations) + ')' if gotS is not None else None}"
    )

    label_s0_last = bt_trueS.sample0_utc + aS / sr + 120.0 / 1000.0

    if gotS is None:
        # ambiguous branch: resolve via the real WWV_20000 sibling
        assert acqS.state == RegistrationAcquirer.STATE_BOOTSTRAP
        open_stations = {h.assignments[0][0] for h in acqS._open}
        print(f"  SHARED_10000 open hypotheses: {sorted(open_stations)}")
        for h in acqS._open:
            print(
                f"    corr={h.correction_s * 1000:+9.3f} ms  sigma={h.sigma_ms:6.3f}  "
                f"support={h.support}  assignments={h.assignments}"
            )
        for h in acqS._open:
            st_h = h.assignments[0][0]
            tol = SAME_SITE_AGREE_MS if st_h in got20.stations else CROSS_SITE_AGREE_MS
            sib_s0 = got20.sample0_utc_for(rtpS)
            frac_ms = (
                wrap_half_second((label_s0_last + h.correction_s) - sib_s0) * 1000.0
            )
            print(
                f"    vs WWV_20000 sibling: {st_h} frac={frac_ms:+9.3f} ms  tol={tol} ms"
            )
        resolved = acqS.resolve_ambiguity(got20, rtpS, label_s0_last)
        print(f"  resolve_ambiguity -> {resolved}")
        assert (
            resolved is not None
        ), "resolve_ambiguity should resolve using the real WWV_20000 sibling"
        assert resolved.stations == ("WWV",)
        shared_plane_rtp, shared_plane_reg = rtpS, resolved
    else:
        # unambiguous branch: SHARED already has its own plane (WWV+BPM
        # together, or WWVH alone) -- fuse it with the WWV_20000 sibling
        # instead of resolving an ambiguity that doesn't exist.
        diff_ms = (gotS.sample0_utc_for(rtpS) - got20.sample0_utc_for(rtpS)) * 1000.0
        print(
            f"  SHARED_10000 vs WWV_20000 acquired-plane difference: {diff_ms:+.3f} ms"
        )
        # Task 6 fix round 1: rtpS is in SHARED_10000's counter; name it.
        fused = fuse_registrations([gotS, got20], rtpS, domain=gotS.channel)
        print(f"  fuse_registrations([shared, wwv20000]) -> {fused}")
        assert (
            fused is not None
        ), "fuse_registrations should not reject same-site planes"
        shared_plane_rtp, shared_plane_reg = rtpS, fused

    # measurement for Michael: how far off is the SHARED sidecar plane from
    # the real tick, whichever station SHARED's own signal names?
    gap_ms = (
        shared_plane_reg.sample0_utc_for(shared_plane_rtp)
        - (bt_trueS.sample0_utc + aS / sr)
    ) * 1000.0
    print(
        f"  SHARED_10000 sidecar/T4 plane vs resolved/fused real plane: {gap_ms:+.3f} ms"
    )

    # ── Either way: fine search for WWV on the resolved/fused plane ──
    bt_acqS = dataclasses.replace(
        bt_trueS,
        sample0_utc=shared_plane_reg.sample0_utc_for(shared_plane_rtp),
        origin_source="acquired",
        origin_sigma_ms=shared_plane_reg.sigma_ms,
    )
    resS = det.detect_edges(
        audio_signal=audioS,
        station="WWV",
        minute_number=minute_utcS,
        buffer_timing=bt_acqS,
        expected_delay_sec=delaysS["WWV"],
        is_dedicated_channel=False,
        iq_samples=None,
    )
    print(
        f"  SHARED_10000 resolved/fused-plane fine search WWV: "
        f"sigma1={resS.sigma_single_ms if resS else float('nan'):.3f} ms  "
        f"n={resS.n_detected if resS else -1}  "
        f"err={resS.ensemble_timing_error_ms if resS else float('nan'):+.3f} ms"
    )
    assert resS is not None
    assert resS.sigma_single_ms < 1.0
    assert resS.n_detected >= 40
    assert abs(resS.ensemble_timing_error_ms) < 2.0


# ── ND same-site siblings (task-11 fix round 4) ────────────────────────
# Real ND WWV_20000 and WWV_25000 chunks for the SAME window as the
# SHARED_10000 fixture above (2026-09-06 21:20-21:30Z), from the same
# radiod and the same RTP counter space.  These exist so the shared
# channel's cross-channel resolution path -- resolve_ambiguity against a
# same-site sibling's plane, then fuse_registrations across the set --
# runs on real signal instead of a synthesized sibling.
ND_SIB_20 = FIX / "wwv_20000-1788729000"
ND_SIB_25 = FIX / "wwv_25000-1788729000"


def _pair_obs(meta: dict):
    """The channel's own radiod ``(GPS_TIME, RTP_TIMESNAP)`` snapshot as a
    ``cross_channel_rtp.PairObservation`` -- the only cross-channel
    evidence radiod offers, and exactly what ``same_counter_space`` wants.
    Prefers the ``timing`` block's ``radiod_*`` copies (what the judge
    actually saw) and falls back to the sidecar's top-level pair."""
    from hf_timestd.core.cross_channel_rtp import PairObservation

    t = meta.get("timing") or {}
    return PairObservation(
        gps_time_ns=int(t.get("radiod_gps_time_ns", meta["gps_time_ns"])),
        rtp_timesnap=int(t.get("radiod_rtp_timesnap", meta["rtp_timesnap"])),
        sample_rate_hz=int(meta["sample_rate"]),
    )


def _sidecar_plane_at(meta: dict, at_rtp: int) -> float:
    """The channel's own labelled plane (sidecar pair + Offset-Judge
    correction, via the production ``resolve_buffer_timing``) evaluated at
    a COMMON RTP.  Valid across channels only because they share one
    counter space -- asserted before this is used."""
    from hf_timestd.core.buffer_timing import resolve_buffer_timing

    sr = int(meta["sample_rate"])
    bt = resolve_buffer_timing(meta, sample_rate=sr)
    return bt.sample0_utc + (int(at_rtp) - int(meta["start_rtp_timestamp"])) / sr


def _own_plane_fine(meta, got, rtp, audio, delays, minute_utc, bt_true, station):
    """Fine search for ``station`` on a channel's OWN acquired plane."""
    from hf_timestd.core.tick_edge_detector import TickEdgeDetector

    sr = int(meta["sample_rate"])
    bt_acq = dataclasses.replace(
        bt_true,
        sample0_utc=got.sample0_utc_for(rtp),
        origin_source="acquired",
        origin_sigma_ms=got.sigma_ms,
    )
    return TickEdgeDetector(sample_rate=sr).detect_edges(
        audio_signal=audio,
        station=station,
        minute_number=minute_utc,
        buffer_timing=bt_acq,
        expected_delay_sec=delays[station],
        is_dedicated_channel=True,
        iq_samples=None,
    )


@pytest.mark.skipif(
    not (ND_SIB_20.with_suffix(".bin.zst").exists())
    or not (ND_SIB_25.with_suffix(".bin.zst").exists()),
    reason="ND same-site sibling fixtures (WWV_20000/WWV_25000) not present",
)
def test_nd_shared_channel_resolved_by_real_same_site_siblings(chunk):
    """Spec §4 cross-channel resolution, on real ND signal.

    A shared channel hearing ONE 1000 Hz tick cannot tell WWV from BPM by
    itself; the design resolves that with a SAME-SITE sibling channel's
    plane (``SAME_SITE_AGREE_MS`` = 1.5 ms; cross-site 4.0 ms).  ND's
    WWV_20000 and WWV_25000 chunks for the identical window, same radiod,
    same RTP counter, are the first real data for that path.

    **Fix-round-4 finding -- neither sibling can do the job in this
    window, and one of them USED TO BE actively dangerous:**

    * ``WWV_20000`` carries no acquirable tick.  Fold-peak SNR never
      crosses the ~13 dB floor at any fold length (1000-band 10.9 dB at
      60 s, 10.1 at 180 s, 9.7 at 540 s), and the acquirer stays
      BOOTSTRAP through every offered minute -- correct behaviour on a
      closed band.
    * ``WWV_25000`` used to acquire, unambiguously, as ``('WWV',)``, on a
      plane **~497 ms wrong** -- a single 13.7 dB fold peak at 502.54 ms
      that existed only at the one fold length the acquirer happened to
      use (122 s); folds of 60/180/540 s over the same chunk find no peak
      at all in either band.  A WWV tick cannot arrive half a second late
      -- the whole ionospheric delay budget is tens of milliseconds -- so
      this was a fold-lattice phantom, and the fine search on the
      resulting plane said so plainly: sigma_1 = 14.6 ms (vs. 0.40 ms for
      SHARED_10000's real tick), well past ``TIMING_SIGMA_MAX_MS`` = 6 ms,
      the design's own "tick-like ensemble" bound.
      The mechanism mattered beyond this fixture: on a single-station
      channel ``fit_template`` marks a lone peak unambiguous by
      construction (there is no second station to confuse it with), so a
      DEDICATED channel on a closed band would self-register on noise
      with no corroboration -- the design's strongest-looking input was
      its weakest when the band was shut.

      **task-11b closes this**: ``_try_acquire`` now requires the winning
      peak to recur, within ``PEAK_PERSISTENCE_MS``, in an independent
      fold of the FIRST half and the SECOND half of the buffer (spec
      §10).  A fold-lattice phantom is absent from at least one half (it
      only ever crossed the detection floor at the one fold length the
      acquirer happened to land on); a genuine tick, present every
      second, survives the halving.  On this exact chunk, WWV_25000 now
      stays BOOTSTRAP outright -- gate (a) alone stops the phantom, never
      reaching the fine-search verification gate (b) at all.  The loop
      below asserts ``gotS is None`` for WWV_25000 UNCONDITIONALLY --
      review fix round 1, I2: an earlier either/or version of this
      assertion (BOOTSTRAP-if-never-acquired, else feed the fine-search
      result to ``verify()`` and require "rejected") kept passing even
      with gate (a) disabled, since gate (b) alone was enough to reject
      the resulting plane -- so it never actually pinned gate (a).  The
      unconditional assert now fails outright if gate (a) ever regresses;
      the (b) fallback after it is unreachable on this fixture and kept
      only in case a future/different WWV_25000 chunk ever acquires
      despite gate (a).

    Everything the fixture CAN establish is asserted; the sibling-resolved
    assertions run only behind a real, tick-like sibling plane, and the
    test ``pytest.skip``s (never xfail) with the numbers when there is
    none."""
    from hf_timestd.core.cross_channel_rtp import epoch_offset_s, same_counter_space
    from hf_timestd.core.registration_acquirer import (
        CROSS_SITE_AGREE_MS,
        RegistrationAcquirer,
        SAME_SITE_AGREE_MS,
        wrap_half_second,
    )
    from hf_timestd.core.registration_store import FUSE_OUTLIER_MS, fuse_registrations

    iq_shared, meta_shared = chunk
    sr = int(meta_shared["sample_rate"])
    iq20, meta20 = _load(FIX, ND_SIB_20.name)
    iq25, meta25 = _load(FIX, ND_SIB_25.name)
    common_rtp = int(meta_shared["start_rtp_timestamp"])
    metas = [
        ("SHARED_10000", meta_shared),
        ("WWV_20000", meta20),
        ("WWV_25000", meta25),
    ]

    # ── Step 0: one counter space?  (precondition for every plane
    # comparison below -- sample0_utc_for is only transferable between
    # channels that share a counter.) ──
    print("\n  === counter space (radiod pair epochs) ===")
    for i in range(len(metas)):
        for j in range(i + 1, len(metas)):
            (na, ma), (nb, mb) = metas[i], metas[j]
            oa, ob = _pair_obs(ma), _pair_obs(mb)
            d_ms = (epoch_offset_s(oa) - epoch_offset_s(ob)) * 1000.0
            ok = same_counter_space(oa, ob)
            print(f"    {na:13s} vs {nb:13s}: epoch diff {d_ms:+8.4f} ms  same={ok}")
            assert ok, f"{na} and {nb} are not in one counter space ({d_ms:+.3f} ms)"

    # ── plane table: every channel's labelled plane at ONE RTP ──
    base_plane = _sidecar_plane_at(meta_shared, common_rtp)
    print(f"  === labelled (sidecar+judge) planes at RTP {common_rtp} ===")
    print(f"    {'channel':13s} {'sample0_utc':>20s} {'minus SHARED (ms)':>19s}")
    plane_by_channel = {}
    for name, m in metas:
        s0 = _sidecar_plane_at(m, common_rtp)
        plane_by_channel[name] = s0
        print(f"    {name:13s} {s0:20.9f} {(s0 - base_plane) * 1000.0:+19.4f}")
    worst_pair, worst_ms = None, 0.0
    for i in range(len(metas)):
        for j in range(i + 1, len(metas)):
            na, nb = metas[i][0], metas[j][0]
            d = abs(plane_by_channel[na] - plane_by_channel[nb]) * 1000.0
            if d > worst_ms:
                worst_pair, worst_ms = (na, nb), d
    print(
        f"    worst pairwise labelled-plane disagreement: {worst_ms:.4f} ms "
        f"({worst_pair[0]} vs {worst_pair[1]}); SAME_SITE_AGREE_MS = "
        f"{SAME_SITE_AGREE_MS} ms"
    )
    # Regression guard against a real counter-space break, at the tolerance
    # cross_channel_rtp itself documents (10 ms) -- NOT at SAME_SITE_AGREE_MS.
    # The 1.5 ms constant is a measurement-quality bound on TICK-derived
    # planes; these are labelled radiod planes, and what they actually
    # measure here is reported above, not asserted against that constant.
    assert worst_ms < 10.0, (
        f"labelled planes disagree by {worst_ms:.3f} ms at a common RTP -- "
        "beyond the documented (GPS_TIME, RTP_TIMESNAP) non-atomicity"
    )

    # ── Step 1: the two dedicated channels, acquired independently ──
    sibling_planes = {}
    acquired_siblings = []
    trusted = []
    narrative = {}  # name -> what was actually measured THIS run (I2: no
    # hard-coded prose in the trailing pytest.skip below)
    for name, iq_s, meta_s in (
        ("WWV_20000", iq20, meta20),
        ("WWV_25000", iq25, meta25),
    ):
        gotS, acqS, rtpS, audioS, delaysS, muS, btS, aS, kS = _acquire_channel(
            iq_s, meta_s, shift_ms=0.0, epoch_id="ep-nd", max_k=5
        )
        if name == "WWV_25000":
            # task-11b fix round 1 (I2): UNCONDITIONAL -- this fixture's
            # WWV_25000 must not acquire at all; gate (a) [peak
            # persistence, spec §10] is the mechanism that stops it.  This
            # must fail if gate (a) ever regresses -- it is not allowed to
            # instead pass via gate (b) rejecting an acquired plane (the
            # reviewer confirmed the previous either/or structure kept
            # passing with gate (a) patched to a no-op).
            assert gotS is None, (
                f"WWV_25000 acquired unambiguously (stations={gotS.stations}, "
                f"sigma={gotS.sigma_ms:.3f} ms, state={acqS.state}) -- gate (a) "
                "peak-persistence should have stopped this fold-lattice "
                "phantom outright; it did not"
            )
        if gotS is None:
            snr = _max_fold_snr_by_band(iq_s, meta_s, sr)
            snr_str = ", ".join(f"{b}:{s:.1f} dB (n={n})" for b, (n, s) in snr.items())
            print(
                f"  {name}: NO acquisition in {kS + 1} offered minutes "
                f"(state={acqS.state}); max fold SNR over the whole chunk = "
                f"{snr_str} -- band closed, acquirer correctly stays BOOTSTRAP"
            )
            assert acqS.state == RegistrationAcquirer.STATE_BOOTSTRAP
            narrative[name] = (
                f"{name} never acquired in {kS + 1} offered minutes "
                f"(state={acqS.state}; max fold SNR {snr_str})"
            )
            continue
        res = _own_plane_fine(
            meta_s, gotS, rtpS, audioS, delaysS, muS, btS, gotS.stations[0]
        )
        if name == "WWV_25000":
            # Unreachable on this fixture (the hard assert above already
            # requires gotS is None here) -- kept as a defensive fallback
            # so gate (b) is still exercised and still rejects if a
            # future/different WWV_25000 fixture ever DOES acquire despite
            # gate (a).
            outcome = acqS.verify(
                {
                    gotS.stations[0]: (
                        res.ensemble_timing_error_ms if res else 0.0,
                        res.sigma_single_ms if res else float("inf"),
                    )
                }
            )
            narrative[name] = (
                f"WWV_25000 acquired despite gate (a) (sigma1="
                f"{res.sigma_single_ms if res else float('nan'):.3f} ms); "
                f"gate (b) verify() -> {outcome}"
            )
            print(f"  {narrative[name]}")
            assert outcome == "rejected"
            assert acqS.state == RegistrationAcquirer.STATE_BOOTSTRAP
            continue
        s0 = gotS.sample0_utc_for(common_rtp)
        sibling_planes[name] = s0
        acquired_siblings.append((name, gotS))
        print(
            f"  {name}: ACQUIRED at minute {kS + 1} stations={gotS.stations} "
            f"sigma={gotS.sigma_ms:.3f} ms; own-plane fine search "
            f"sigma1={res.sigma_single_ms if res else float('nan'):.3f} ms "
            f"n={res.n_detected if res else -1} "
            f"err={res.ensemble_timing_error_ms if res else float('nan'):+.3f} ms; "
            f"plane minus SHARED labelled plane = {(s0 - base_plane) * 1000.0:+.3f} ms"
        )
        # A single-station channel must name exactly its own station.
        assert gotS.stations == ("WWV",)
        # Is this plane worth trusting as a sibling?  The design's own
        # tick-likeness bound (RegistrationAcquirer.TIMING_SIGMA_MAX_MS,
        # = TickEdgeDetector.LABEL_ANCHOR_MAX_SIGMA_MS) decides -- an
        # ensemble whose per-tick scatter exceeds it is not a tick train.
        tick_like = res is not None and (
            res.sigma_single_ms <= RegistrationAcquirer.TIMING_SIGMA_MAX_MS
        )
        if tick_like:
            trusted.append((name, gotS, meta_s))
        else:
            print(
                f"    -> NOT tick-like: sigma1 "
                f"{res.sigma_single_ms if res else float('nan'):.3f} ms > "
                f"TIMING_SIGMA_MAX_MS={RegistrationAcquirer.TIMING_SIGMA_MAX_MS} ms. "
                "This registration is a fold-lattice phantom, not a tick lock; "
                "it is NOT offered as a sibling."
            )

    # ── Step 2: SHARED_10000 with the label shifted -200 ms.  Two peaks
    # (WWV + BPM) or one?  Take both branches the design allows. ──
    shift_ms = -200.0
    boot = _acquire_channel(
        iq_shared, meta_shared, shift_ms=shift_ms, epoch_id="ep-nd", max_k=2
    )
    gotB, acqB, rtpB, _aud, _dly, _mu, btB, aB, kB = boot
    label_s0_boot = btB.sample0_utc + aB / sr + shift_ms / 1000.0
    print(
        f"  SHARED_10000 (shift {shift_ms:+.0f} ms) after {kB + 1} minutes: "
        f"state={acqB.state} open_hypotheses={len(acqB._open)} "
        f"stations={sorted({h.assignments[0][0] for h in acqB._open})}"
    )
    for h in acqB._open:
        print(
            f"    open: corr={h.correction_s * 1000:+9.3f} ms sigma={h.sigma_ms:6.3f} "
            f"support={h.support} station={h.assignments[0][0]}"
        )

    # ── the cross-channel comparison, exercised in EITHER case ──
    print("  === cross-channel comparison ===")
    for name, s0 in sibling_planes.items():
        for h in acqB._open:
            st_h = h.assignments[0][0]
            tol = SAME_SITE_AGREE_MS if st_h == "WWV" else CROSS_SITE_AGREE_MS
            frac_ms = wrap_half_second((label_s0_boot + h.correction_s) - s0) * 1000.0
            print(
                f"    BOOTSTRAP hyp {st_h:4s} vs {name}: frac={frac_ms:+9.3f} ms "
                f"tol={tol} ms  -> {'within' if abs(frac_ms) <= tol else 'REJECTED'}"
            )
    # Offer EVERY acquired sibling to resolve_ambiguity, tick-like or not --
    # what the gate does with a bad sibling is exactly as interesting as what
    # it does with a good one, and a rejected sibling leaves the acquirer's
    # state untouched (resolve_ambiguity returns before it mutates anything).
    resolved_by = {}
    for name, sib in acquired_siblings:
        trust = "tick-like" if any(n == name for n, _g, _m in trusted) else "PHANTOM"
        resolved_by[name] = acqB.resolve_ambiguity(sib, rtpB, label_s0_boot)
        print(
            f"    resolve_ambiguity({name}, {trust}) -> "
            f"{'None' if resolved_by[name] is None else resolved_by[name].stations}"
        )

    got_alone = _acquire_channel(
        iq_shared, meta_shared, shift_ms=shift_ms, epoch_id="ep-nd", max_k=3
    )
    gotS10, acqS10, rtpS10, audioS10, delaysS10, muS10, btS10, aS10, kS10 = got_alone
    if gotS10 is not None:
        print(
            f"  SHARED_10000 ACQUIRED ALONE at minute {kS10 + 1}: "
            f"stations={gotS10.stations} sigma={gotS10.sigma_ms:.3f} ms; plane minus "
            f"labelled plane = "
            f"{(gotS10.sample0_utc_for(rtpS10) - (btS10.sample0_utc + aS10 / sr)) * 1000.0:+.3f} ms"
        )
        assert "WWV" in gotS10.stations
        for name, s0 in sibling_planes.items():
            d_ms = (gotS10.sample0_utc_for(common_rtp) - s0) * 1000.0
            print(f"    SHARED acquired plane vs {name} acquired plane: {d_ms:+.3f} ms")

    # ── Step 3: fuse.  First literally everything that acquired (the task's
    # own wording), then the trusted subset the design would actually offer. ──
    every_reg = [r for r in [gotS10] + [g for _n, g in acquired_siblings] if r]
    # Task 6 fix round 1: common_rtp is in the shared channel's counter.
    shared_domain = str(meta_shared["channel_name"])
    every_fused = fuse_registrations(every_reg, common_rtp, domain=shared_domain)
    print(
        f"  fuse_registrations(ALL acquired {[r.channel for r in every_reg]}) -> "
        f"{'None' if every_fused is None else f'sigma={every_fused.sigma_ms:.3f}'}"
    )
    if len(every_reg) > 1:
        utc_all = [r.sample0_utc_for(common_rtp) for r in every_reg]
        med_all = float(np.median(utc_all))
        for r, u in zip(every_reg, utc_all):
            print(
                f"    {r.channel}: {(u - med_all) * 1000.0:+.3f} ms from the median "
                f"(FUSE_OUTLIER_MS={FUSE_OUTLIER_MS}) -> "
                f"{'kept' if abs(u - med_all) * 1000.0 <= FUSE_OUTLIER_MS else 'REJECTED'}"
            )
    all_regs = [r for r in [gotS10] + [g for _n, g, _m in trusted] if r is not None]
    fused = fuse_registrations(all_regs, common_rtp, domain=shared_domain)
    print(
        f"  fuse_registrations({[r.channel for r in all_regs]}) -> "
        f"{'None' if fused is None else f'sigma={fused.sigma_ms:.3f} stations={fused.stations}'}"
    )
    if fused is None and len(all_regs) > 1:
        utc = [r.sample0_utc_for(common_rtp) for r in all_regs]
        med = float(np.median(utc))
        for r, u in zip(all_regs, utc):
            print(
                f"    {r.channel}: {(u - med) * 1000.0:+.3f} ms from the median "
                f"(FUSE_OUTLIER_MS={FUSE_OUTLIER_MS}) -> "
                f"{'kept' if abs(u - med) * 1000.0 <= FUSE_OUTLIER_MS else 'REJECTED'}"
            )

    if not trusted:
        # I2 (task-11b fix round 1): built from what THIS run actually
        # measured (`narrative`), not asserted in prose which gate fired --
        # the reviewer showed the previous hard-coded text kept narrating
        # "gate (a) stopped it" even with gate (a) patched to a no-op.
        pytest.skip(
            "no ND same-site sibling supplies a trustworthy plane in this window: "
            + "; ".join(
                narrative.get(n, f"{n}: no data") for n in ("WWV_20000", "WWV_25000")
            )
            + ".  The cross-channel machinery was still exercised above and "
            "behaved correctly: no phantom was offered as a sibling, and "
            "fuse_registrations did not silently average a bad plane in.  The "
            "sibling-resolved assertions below need a window where 20 or 25 MHz "
            "is actually open at ND -- see the task-11 report, fix round 4, and "
            "the task-11b report."
        )

    # ── With a real, tick-like sibling: the design's own claims ──
    sib_name, sib_reg, _sib_meta = trusted[0]
    if gotS10 is None:
        resolved = resolved_by.get(sib_name)
        assert resolved is not None, (
            f"resolve_ambiguity should resolve the {{WWV, BPM}} ambiguity from the "
            f"real {sib_name} sibling plane"
        )
        assert resolved.stations == ("WWV",)
        plane_reg, plane_rtp = resolved, rtpB
    else:
        agree_ms = (
            gotS10.sample0_utc_for(common_rtp) - sibling_planes[sib_name]
        ) * 1000.0
        assert abs(agree_ms) <= SAME_SITE_AGREE_MS, (
            f"SHARED_10000's own acquired plane disagrees with same-site "
            f"{sib_name} by {agree_ms:+.3f} ms (> {SAME_SITE_AGREE_MS} ms)"
        )
        plane_reg, plane_rtp = gotS10, rtpS10

    assert fused is not None, "fuse_registrations rejected every same-site plane"
    assert "WWV" in fused.stations
    for r in all_regs:
        d_ms = (
            r.sample0_utc_for(common_rtp) - fused.sample0_utc_for(common_rtp)
        ) * 1000.0
        assert abs(d_ms) < FUSE_OUTLIER_MS, (
            f"{r.channel} sits {d_ms:+.3f} ms from the fused plane -- rejected as an "
            "outlier"
        )

    # ── the fine search for WWV on SHARED_10000 under the fused plane ──
    from hf_timestd.core.tick_edge_detector import TickEdgeDetector

    bt_fused = dataclasses.replace(
        btS10,
        sample0_utc=fused.sample0_utc_for(rtpS10),
        origin_source="acquired",
        origin_sigma_ms=fused.sigma_ms,
    )
    res_f = TickEdgeDetector(sample_rate=sr).detect_edges(
        audio_signal=audioS10,
        station="WWV",
        minute_number=muS10,
        buffer_timing=bt_fused,
        expected_delay_sec=delaysS10["WWV"],
        is_dedicated_channel=False,
        iq_samples=None,
    )
    print(
        f"  SHARED_10000 fused-plane fine search WWV (plane from {plane_reg.channel} "
        f"@ rtp {plane_rtp}): sigma1="
        f"{res_f.sigma_single_ms if res_f else float('nan'):.3f} ms "
        f"n={res_f.n_detected if res_f else -1} "
        f"err={res_f.ensemble_timing_error_ms if res_f else float('nan'):+.3f} ms"
    )
    assert res_f is not None
    assert res_f.sigma_single_ms < 1.0
    assert res_f.n_detected >= 40
    assert abs(res_f.ensemble_timing_error_ms) < 2.0
