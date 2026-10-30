"""Task 10: metrology runs T3 stand-alone on every station.

T3 starts from radiod's raw (GPS_TIME, RTP_TIMESNAP) pair, which the ring
carries as ``raw_gps_time_ns`` / ``raw_rtp_timesnap`` (Task 4), and checks
its plane against this channel's own fold and minute marker
(``RegistrationAcquirer.measure_minute``, Task 9).  The ring label -- T6's
plane on a T6 station -- is a recorded diagnostic and nothing more: it never
verifies the plane, never resets it, and never names its second.

The harness drives ``apply_registration`` minute by minute.  The ticks sit
at truth.  The raw pair sits at truth + ``raw_offset``.  The ring label
(``judge_tier="T6"`` unless ``t6_label_offset`` is None) sits at truth +
``t6_label_offset``, a plane T3 must never consult.
"""


import logging

import pytest

from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns
from hf_timestd.core.counter_epoch_tracker import CounterEpochTracker
from hf_timestd.core.metrology_service import MetrologyService
from hf_timestd.core.registration_acquirer import RegistrationAcquirer
from hf_timestd.core.registration_store import RegistrationStore
from tests.unit.synth_origins import ND_K0, counter_map_for
from tests.unit.synth_ticks import label_timing, make_tick_audio

SR = 24000
MIN = 1_800_000_000
T0 = MIN - 1  # buffer k starts 1 s before minute k (truth) and lasts 62 s
RTP0 = 1_000_000
DELAYS = {"WWV": 0.0125}
# A T3-only station's ring label still sits somewhere other than truth; it
# carries no tier, so it is nobody's authority.
_UNTIERED_RING_OFFSET = 0.040
# The raw pair is a snapshot taken half a second before the buffer starts,
# so the label is built through an RTP difference, not read off directly.
_SNAP_LEAD = SR // 2


class _Engine:
    sample_rate = SR

    def prepare_audio(self, iq):
        import numpy as np

        return np.asarray(iq, dtype=np.float64)

    def expected_delays_s(self, system_time, utc_minute):
        return dict(DELAYS)


def truth(k: int) -> float:
    """Minute k's true sample-0 UTC."""
    return float(T0 + 60 * k)


def rtp_of_minute(k: int) -> int:
    """Minute k's start RTP, in this channel's counter."""
    return (RTP0 + 60 * k * SR) % 2**32


def _gps_ns(utc_whole_s: int, frac_s: float) -> int:
    """GPS_TIME ns for UTC ``utc_whole_s + frac_s``, in integers so the
    nanoseconds survive (a float near 1.8e18 ns keeps ~256 ns)."""
    return unix_ns_to_gps_time_ns(utc_whole_s * 10**9 + int(round(frac_s * 1e9)))


def _service(tmp_path):
    svc = MetrologyService.__new__(MetrologyService)  # bypass the heavy __init__
    svc.channel_name = "WWV_10000"
    svc.sample_rate = SR
    svc.engine = _Engine()
    svc.acquirer = RegistrationAcquirer("WWV_10000", SR)
    svc.reg_store = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    svc.epoch_tracker = CounterEpochTracker()
    svc._AUTHORITY_JSON_PATH = tmp_path / "no-authority.json"
    return svc


def minute_metadata(k, raw_offset, ring_offset):
    """Minute k's ring metadata.  ``gps_time_ns`` / ``rtp_timesnap`` are the
    ring anchor (the label plane); the raw pair is radiod's own."""
    rtp = rtp_of_minute(k)
    meta = {
        "start_rtp_timestamp": rtp,
        "gps_time_ns": _gps_ns(T0 + 60 * k, ring_offset),
        "rtp_timesnap": rtp,
        "sample_rate": SR,
        "counter_map": counter_map_for(ND_K0[0], SR),
        "raw_gps_time_ns": None,
        "raw_rtp_timesnap": None,
    }
    if raw_offset is not None:
        meta["raw_gps_time_ns"] = _gps_ns(T0 + 60 * k, raw_offset - _SNAP_LEAD / SR)
        meta["raw_rtp_timesnap"] = (rtp - _SNAP_LEAD) % 2**32
    return meta


def run_minutes(tmp_path, raw_offset, t6_label_offset, n, marker=True):
    """Feed ``n`` minutes; keep the last BufferTiming as ``svc.last_bt``."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    svc = _service(tmp_path)
    ring_offset = (
        _UNTIERED_RING_OFFSET if t6_label_offset is None else t6_label_offset
    )
    for k in range(n):
        audio = make_tick_audio(
            62, SR, truth(k), DELAYS, snr_db=20.0, seed=7 + k, marker=marker
        )
        label = label_timing(truth(k), ring_offset, SR)
        if t6_label_offset is not None:
            label.judge_tier = "T6"
        svc.last_bt = svc.apply_registration(
            label, audio, rtp_of_minute(k), MIN + 60 * k,
            minute_metadata(k, raw_offset, ring_offset),
        )
    return svc


@pytest.mark.parametrize("raw_offset", [0.007, -0.013, 1.000])
def test_t3_recovers_truth_from_an_offset_raw_pair(tmp_path, raw_offset):
    svc = run_minutes(tmp_path, raw_offset=raw_offset, t6_label_offset=0.040, n=6)
    s = svc.reg_store.read_summary()
    assert s["state"] == "WITNESS"
    assert svc.acquirer.registration.sample0_utc_for(rtp_of_minute(5)) == pytest.approx(
        truth(5), abs=0.001
    )
    # today: CANDIDATE and reset every 3 minutes ("disagrees with T6")


def test_registration_ignores_the_t6_plane(tmp_path):
    a = run_minutes(tmp_path / "a", raw_offset=0.007, t6_label_offset=0.0, n=6)
    b = run_minutes(tmp_path / "b", raw_offset=0.007, t6_label_offset=0.040, n=6)
    assert a.acquirer.registration.utc_ref == b.acquirer.registration.utc_ref
    assert a.reg_store.read_summary()["state"] == b.reg_store.read_summary()["state"]


def test_the_second_comes_from_the_raw_pair_not_the_ring_label(tmp_path):
    svc = run_minutes(tmp_path, raw_offset=0.007, t6_label_offset=1.000, n=4, marker=False)
    assert svc.acquirer.registration.sample0_utc_for(rtp_of_minute(3)) == pytest.approx(
        truth(3), abs=0.001
    )


def test_no_raw_pair_means_no_registration(tmp_path):
    s = run_minutes(tmp_path, raw_offset=None, t6_label_offset=0.0, n=2).reg_store.read_summary()
    assert s["state"] == "BOOTSTRAP" and s["standalone_reason"] == "raw_pair_unavailable"


def test_t3_only_station_runs_the_same_code(tmp_path):
    svc = run_minutes(tmp_path, raw_offset=0.007, t6_label_offset=None, n=6)  # judge_tier None
    assert svc.reg_store.read_summary()["state"] == "ACQUIRED" and svc.last_bt.origin_source == "acquired"


def test_t6_residual_is_published_and_never_resets(tmp_path, caplog):
    # INFO: the acquirer logs an ordinary reset at INFO, and today's T6
    # reset ("disagrees with T6") is one of those.
    caplog.set_level(logging.INFO)
    svc = run_minutes(tmp_path, raw_offset=0.007, t6_label_offset=0.040, n=6)
    assert svc.reg_store.read_summary()["residual_vs_t6_ms"] == pytest.approx(-40.0, abs=1.0)
    assert "disagrees with T6" not in caplog.text
    assert "registration reset" not in caplog.text
