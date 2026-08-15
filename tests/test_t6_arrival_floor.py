"""The T6 arrival hand-off must report the least-delayed arrival, not the latest.

Measured on B4 2026-08-14/15: the T6 bench read 27.7 ms early (published
shadow_residual vs T4, n=32, stdev 4.69 ms, bounded in [-37.5, -20.0] ms).
The anchor is not at fault -- chain_delay was 18 ns and the fine-stage
residual -4.87 us the same hour.  The whole error is the arrival hand-off.

radiod emits each 20 ms block as a burst of 11 packets (96 kHz, 10x180 +
1x120 = 1920 samples; 90.9% of inter-arrivals under 0.5 ms).  MultiStream
delivers every `deliver_interval` = 10 packets, so the delivery boundary
precesses through the burst and the labelled sample's age is uniform over
one blocktime -- an offline replica of the real resequencer over 6,599
deliveries measured stdev 5.90 ms with deciles 0.0 .. 19.2 ms.

Taking the arrival with the LEAST latency in a rolling window collapses
that: the same replica measured a floor spread of 0.75 ms at a 2 s window
(0.54 ms at 5 s) against a gate budget of 1.277 ms.

The window must be fed on the arrival path (55 deliveries/s), NOT from
OffsetJudge.poll() -- the judge ticks every 10 s and would see a single
arrival per window, filtering nothing.
"""
import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2, newest_sample_rtp
from hf_timestd.core.native_anchor import NativeAnchor
from hf_timestd.core.offset_judge import NativeAnchorBench
from hf_timestd.core.t6_arrival_floor import ArrivalFloorTracker, FloorEstimate

# True mono->UTC offset used throughout: an arrival delayed by L seconds
# carries the label (mono + TRUE_OFF - L), so its reported offset is
# TRUE_OFF - L and the least-delayed arrival reports the LARGEST offset.
TRUE_OFF = 1_786_000_000.0


def test_floor_is_the_least_delayed_arrival_in_the_window():
    t = ArrivalFloorTracker(window_s=2.0)
    for mono, latency in [(100.0, 0.020), (100.1, 0.012),
                          (100.2, 0.002), (100.3, 0.018)]:
        t.note(TRUE_OFF - latency, mono)

    est = t.estimate(100.3)

    assert est is not None
    # The 2 ms arrival wins; the 18 ms latest one must not.
    assert TRUE_OFF - est.offset_s == pytest.approx(0.002, abs=1e-5)


def test_a_stale_floor_leaves_the_window():
    """A lucky low-latency arrival must not pin the estimate forever."""
    t = ArrivalFloorTracker(window_s=2.0)
    t.note(TRUE_OFF - 0.002, 100.0)     # the lucky one
    t.note(TRUE_OFF - 0.018, 101.0)
    t.note(TRUE_OFF - 0.015, 103.5)     # 100.0 is now 3.5 s old

    est = t.estimate(103.5)

    assert TRUE_OFF - est.offset_s == pytest.approx(0.015, abs=1e-5)


def _run(tracker, floors, per_tick=50, tick_s=1.0, mono0=100.0):
    """Drive `tracker` one judge tick per entry in `floors`.

    Each tick delivers `per_tick` arrivals whose latencies ramp a
    blocktime above that tick's floor -- the measured B4 shape.  Returns
    the final estimate.
    """
    mono, est = mono0, None
    for floor in floors:
        for j in range(per_tick):
            latency = floor + 0.020 * (j / per_tick)
            tracker.note(TRUE_OFF - latency, mono)
            mono += tick_s / per_tick
        est = tracker.estimate(mono)
    return est


def test_sigma_reflects_how_much_the_floor_actually_moves():
    """Sigma must be MEASURED, not asserted.

    The 25 ms LATENCY_SIGMA_FLOOR_NS was unpromotable precisely because
    it was a constant nobody measured: it could never shrink no matter
    how good the instrument got.
    """
    steady = _run(ArrivalFloorTracker(window_s=1.0), [0.002] * 20)
    jittery = _run(ArrivalFloorTracker(window_s=1.0), [0.002, 0.008] * 10)

    assert steady.sigma_ns < jittery.sigma_ns


def test_sigma_never_claims_better_than_the_hand_off_floor():
    """A steady floor has zero scatter -- but zero uncertainty is a lie.

    The hand-off's own constant delay (radiod emit -> kernel -> Python
    timestamp) is invisible from inside the stream: every arrival
    carries it equally, so no amount of filtering reveals it.  What
    catches it is the cross-bench gate against T4/T5, and the residual
    is published in shadow_residuals.  Until then the bench must not
    claim a precision it cannot support.
    """
    est = _run(ArrivalFloorTracker(window_s=1.0), [0.002] * 20)

    assert est.sigma_ns == pytest.approx(ArrivalFloorTracker.MIN_SIGMA_NS)


def test_an_anchor_recapture_discards_the_old_frame():
    """Offsets are relative to the anchor; smoothing across a recapture
    would blend two different frames into one bogus floor."""
    t = ArrivalFloorTracker(window_s=10.0)
    t.note(TRUE_OFF - 0.002, 100.0)
    t.estimate(100.0)

    t.reset("anchor recaptured")
    t.note(TRUE_OFF - 0.015, 100.5)
    est = t.estimate(100.5)

    assert TRUE_OFF - est.offset_s == pytest.approx(0.015, abs=1e-5)
    # sigma must not be inherited from the discarded frame either.  It
    # cannot be MEASURED from one sample, and inf would publish as
    # invalid JSON (`Infinity`) into shadow_residuals, so the honest
    # wide transport bound stands in until scatter is observable.
    assert est.sigma_ns == ArrivalFloorTracker.UNMEASURED_SIGMA_NS


# ────────────────────────────────────────────────────────────────────
# NativeAnchorBench consuming the floor
# ────────────────────────────────────────────────────────────────────

SR = 96000


def _anchor(truth_utc, anchor_rtp=5000):
    ns = int(round(truth_utc * 1e9))
    return NativeAnchor(
        anchor_rtp=anchor_rtp & 0xFFFFFFFF, anchor_utc_ns=ns,
        sample_rate_hz=SR, chain_delay_ns=0,
        captured_at_utc_ns=ns, captured_via_tier="T5",
    )


def test_bench_reads_the_floor_not_the_latest_arrival():
    """The whole point: a 20 ms-late latest arrival must not set the clock."""
    mono = 100.0
    anchor = _anchor(truth_utc=mono + TRUE_OFF)
    # Latest arrival is a full blocktime late -- today's bench would
    # publish exactly this error.
    late_rtp = 5000 - int(round(0.020 * SR))
    floor = FloorEstimate(offset_s=TRUE_OFF - 0.002, sigma_ns=750_000.0,
                          n=110, span_s=2.0)
    bench = NativeAnchorBench(
        provider=lambda: (anchor, late_rtp, mono, floor),
        mono_fn=lambda: mono,
    )

    r = bench.poll()

    assert r is not None and r.tier == "T6"
    error_s = (mono + TRUE_OFF) - r.utc_at(mono)
    assert error_s == pytest.approx(0.002, abs=1e-5)
    assert r.sigma_ns == pytest.approx(750_000.0)


def test_bench_without_a_floor_keeps_the_conservative_bound():
    """No floor yet (startup, or a just-reset frame) => today's behavior:
    the latest arrival with the honest 25 ms transport bound."""
    mono = 100.0
    anchor = _anchor(truth_utc=mono + TRUE_OFF)
    bench = NativeAnchorBench(
        provider=lambda: (anchor, 5000, mono), mono_fn=lambda: mono)

    r = bench.poll()

    assert r is not None
    assert r.sigma_ns == pytest.approx(NativeAnchorBench.LATENCY_SIGMA_FLOOR_NS)


# ────────────────────────────────────────────────────────────────────
# The arrival label itself
# ────────────────────────────────────────────────────────────────────

def _quality(**kw):
    import types as _t
    base = dict(delivered_rtp_start=None, batch_samples_delivered=0,
                last_rtp_timestamp=0)
    base.update(kw)
    return _t.SimpleNamespace(**base)


def test_newest_sample_label_is_the_end_of_the_delivered_batch():
    """`last_rtp_timestamp` is the last RECEIVED packet's header, stamped
    pre-resequencer.  ka9q-python added `delivered_rtp_start` to replace
    exactly this use (stream_quality.py: "Prefer this over
    last_rtp_timestamp for sample labelling"), citing the hf-timestd T6
    origin slips of 2026-08-11.  The T6 arrival path never adopted it.
    """
    q = _quality(delivered_rtp_start=1000, batch_samples_delivered=1800,
                 last_rtp_timestamp=2620)

    assert newest_sample_rtp(q) == 2800


def test_label_falls_back_to_the_received_header_when_batch_label_absent():
    """A producer predating `delivered_rtp_start` must still be labelled --
    with the old, worse label rather than a crash."""
    q = _quality(delivered_rtp_start=None, last_rtp_timestamp=2620)

    assert newest_sample_rtp(q) == 2620


def test_label_wraps_at_32_bits():
    """RTP is a 32-bit counter; the batch end can cross the wrap."""
    q = _quality(delivered_rtp_start=0xFFFFFF00, batch_samples_delivered=0x200)

    assert newest_sample_rtp(q) == 0x100


# ────────────────────────────────────────────────────────────────────
# Recorder wiring
# ────────────────────────────────────────────────────────────────────

def _bare_recorder():
    """CoreRecorderV2 without __init__ -- the house pattern for exercising
    one method (see test_core_recorder_t6_shared.py)."""
    return CoreRecorderV2.__new__(CoreRecorderV2)


def test_bench_state_carries_the_floor_to_the_bench():
    import types as _t
    rec = _bare_recorder()
    rec._t6_native_anchor = _anchor(truth_utc=100.0 + TRUE_OFF)
    rec._t5_pairing = _t.SimpleNamespace(
        latest_arrival=(5000, 100.0), now_mono=lambda: 100.0)
    rec._t6_arrival_floor = ArrivalFloorTracker(window_s=2.0)
    rec._t6_arrival_floor.note(TRUE_OFF - 0.002, 100.0)

    state = rec._t6_bench_state()

    assert state is not None and len(state) == 4
    assert state[3].offset_s == pytest.approx(TRUE_OFF - 0.002, abs=1e-5)


def test_arrival_note_uses_the_batch_end_label_and_feeds_the_floor():
    import types as _t
    seen = []
    rec = _bare_recorder()
    # Anchor labels RTP 5000 as monotonic 100.0's true UTC.
    rec._t6_native_anchor = _anchor(truth_utc=100.0 + TRUE_OFF)
    rec._t5_pairing = _t.SimpleNamespace(
        note_arrival=lambda rtp: seen.append(rtp), now_mono=lambda: 100.0)
    rec._t6_arrival_floor = ArrivalFloorTracker(window_s=2.0)
    # Batch of 1800 samples ENDING at 5000 -- last received header would
    # have been 4820, a packet earlier.
    q = _quality(delivered_rtp_start=5000 - 1800,
                 batch_samples_delivered=1800, last_rtp_timestamp=4820)

    rec._t6_note_arrival(q)

    assert seen == [5000]
    est = rec._t6_arrival_floor.estimate(100.0)
    assert TRUE_OFF - est.offset_s == pytest.approx(0.0, abs=1e-5)


def test_anchor_recapture_also_resets_the_arrival_floor():
    """The floor is expressed against the anchor, exactly like the P3
    rate window, so it must be discarded on the same event."""
    rec = _bare_recorder()
    rec._t6_rate_est = None
    rec._t6_arrival_floor = ArrivalFloorTracker(window_s=10.0)
    rec._t6_arrival_floor.note(TRUE_OFF - 0.002, 100.0)

    rec._t6_rate_reset("anchor recaptured")

    assert rec._t6_arrival_floor.estimate(100.0) is None
