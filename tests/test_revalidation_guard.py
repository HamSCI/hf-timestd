"""Revalidation must read radiod's pair atomically, never from torn legacy fields."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from ka9q.discovery import ChannelInfo
from hf_timestd.core.stream_recorder_v2 import StreamRecorderConfig, StreamRecorderV2

SAMPLE_RATE = 24_000
REF_GPS_NS = 2_000_000_000
REF_RTP = 48_000


def _torn_ci():
    ci = ChannelInfo.__new__(ChannelInfo)
    ci._anchor_pair = (REF_GPS_NS, REF_RTP)             # consistent pair
    ci.gps_time, ci.rtp_timesnap = 3_000_000_000, REF_RTP   # torn: +1 s
    return ci


def test_revalidation_ignores_torn_legacy_fields():
    cfg = StreamRecorderConfig(
        ssrc=None, frequency_hz=7_850_000, sample_rate=SAMPLE_RATE, preset='iq',
        encoding=4, agc_enable=0, gain=0.0, description='TEST_CHU_7850',
        output_dir=Path('/tmp'), receiver_grid='AA00aa', station_config={},
        archive=False, ring_seconds=0,
    )
    sr = StreamRecorderV2(config=cfg, control=MagicMock())

    def evaluate_pair(gps, rtp):
        # fresh_utc - implied_utc; the leap offset cancels against the
        # mapping adopted from (REF_GPS_NS, REF_RTP).
        return gps / 1e9 - (REF_GPS_NS / 1e9 + (rtp - REF_RTP) / SAMPLE_RATE)

    writer = MagicMock()
    writer.evaluate_pair.side_effect = evaluate_pair
    sr.archive_writer = writer
    sr.channel_info = _torn_ci()

    sr.revalidate_radiod_pair()

    writer.evaluate_pair.assert_called_once_with(REF_GPS_NS, REF_RTP)
    writer.add_timing_snapshot.assert_not_called()


# ---------------------------------------------------------------------------
# Task 3: the pair-step arbiter decides adoption; a round trip restores the
# mapping in force before the step.
# ---------------------------------------------------------------------------

import logging

from hamsci_dsp.counter_map import CounterMap

from hf_timestd.core.native_anchor import NativeAnchor
from hf_timestd.core.pair_step_arbiter import T6Reference, pair_disagreement_s
from hf_timestd.core.leap_second import get_current_gps_leap_seconds

# A realistic mapping: radiod's GPS_TIME in 2026 (GPS ns, no leap seconds).
MAP_GPS_NS = 1_450_000_000 * 10**9
MAP_RTP = 1_000_000
GPS_TO_UTC_S = 315964800 - get_current_gps_leap_seconds()


def _recorder():
    cfg = StreamRecorderConfig(
        ssrc=None, frequency_hz=7_850_000, sample_rate=SAMPLE_RATE, preset='iq',
        encoding=4, agc_enable=0, gain=0.0, description='TEST_CHU_7850',
        output_dir=Path('/tmp'), receiver_grid='AA00aa', station_config={},
        archive=False, ring_seconds=0,
    )
    return StreamRecorderV2(config=cfg, control=MagicMock())


def _mapping_writer(mapping):
    """MagicMock writer driven by one stored (gps_time_ns, rtp_timesnap)."""
    writer = MagicMock()
    writer.evaluate_pair.side_effect = lambda g, r: pair_disagreement_s(
        g, r, mapping[0], mapping[1], SAMPLE_RATE)
    writer.current_mapping.side_effect = lambda: tuple(mapping)

    def adopt(gps_time_ns, rtp_timesnap):
        mapping[:] = [gps_time_ns, rtp_timesnap]
        return True
    writer.add_timing_snapshot.side_effect = adopt
    writer.restore_timing_mapping.side_effect = adopt
    return writer


def _pair_ci(gps_ns, rtp):
    ci = ChannelInfo.__new__(ChannelInfo)
    ci._anchor_pair = (gps_ns, rtp)
    ci.gps_time, ci.rtp_timesnap = gps_ns, rtp
    return ci


def _t6_reference_on_mapping():
    """A GPSDO reference that agrees with (MAP_GPS_NS, MAP_RTP), on a
    counter map shared with the channel (identity: one sample, one index)."""
    cmap = CounterMap(rtp_ref=MAP_RTP, input_ref=0, ilen=1, olen=1,
                      input_samprate=SAMPLE_RATE,
                      input_epoch_gps_s=MAP_GPS_NS / 1e9 - MAP_RTP / SAMPLE_RATE,
                      n_agree=3)
    utc_ns = MAP_GPS_NS + GPS_TO_UTC_S * 10**9
    anchor = NativeAnchor(anchor_rtp=MAP_RTP, anchor_utc_ns=utc_ns,
                          sample_rate_hz=SAMPLE_RATE, chain_delay_ns=0,
                          captured_at_utc_ns=utc_ns, captured_via_tier="T6")
    return cmap, T6Reference(anchor=anchor, t6_map=cmap)


def test_t6_refusal_leaves_mapping_untouched(caplog):
    sr = _recorder()
    writer = _mapping_writer([MAP_GPS_NS, MAP_RTP])
    sr.archive_writer = writer
    cmap, ref = _t6_reference_on_mapping()
    sr.publish_counter_map(cmap)
    sr.set_t6_reference_provider(lambda: ref)
    # 60 s on, the pair claims +0.998 s: the ND torn read.
    sr.channel_info = _pair_ci(MAP_GPS_NS + 60_998_000_000,
                               MAP_RTP + 60 * SAMPLE_RATE)

    with caplog.at_level(logging.WARNING):
        sr.revalidate_radiod_pair()

    writer.add_timing_snapshot.assert_not_called()
    assert any(r.levelno == logging.ERROR and 'PAIR STEP REFUSED' in r.getMessage()
               for r in caplog.records)


def test_no_t6_round_trip_nothing_adopted(caplog):
    sr = _recorder()
    writer = _mapping_writer([MAP_GPS_NS, MAP_RTP])
    sr.archive_writer = writer
    sr.ring_buffer = MagicMock()

    with caplog.at_level(logging.WARNING):
        sr.channel_info = _pair_ci(MAP_GPS_NS + 60_998_000_000,
                                   MAP_RTP + 60 * SAMPLE_RATE)
        sr.revalidate_radiod_pair()
        sr.channel_info = _pair_ci(MAP_GPS_NS + 120_000_000_000,
                                   MAP_RTP + 120 * SAMPLE_RATE)
        sr.revalidate_radiod_pair()

    writer.add_timing_snapshot.assert_not_called()
    sr.ring_buffer.update_anchor.assert_not_called()
    assert any('PAIR STEP HELD (awaiting second tick)' in r.getMessage()
               for r in caplog.records)


def test_round_trip_restores_exact_prior_mapping(caplog):
    sr = _recorder()
    writer = _mapping_writer([MAP_GPS_NS, MAP_RTP])
    sr.archive_writer = writer
    sr.ring_buffer = MagicMock()
    # The ring was anchored on the mapping with a judge offset folded in.
    saved_offset_ns = 12_345_678.0
    sr._ring_anchor_state = (MAP_GPS_NS, MAP_RTP, saved_offset_ns)
    # The judge has since moved; the restore must not pick its offset up.
    sr._offset_judge = MagicMock()
    sr._offset_judge.offset_for.return_value = MagicMock(offset_ns=99e6)
    sr._judge_source_key = ('TEST', 1)

    bad = (MAP_GPS_NS + 60_998_000_000, MAP_RTP + 60 * SAMPLE_RATE)
    sr._adopt_pair_step(*bad, 'persistent')
    writer.add_timing_snapshot.assert_called_once_with(
        gps_time_ns=bad[0], rtp_timesnap=bad[1])
    sr.ring_buffer.reset_mock()

    sr.channel_info = _pair_ci(MAP_GPS_NS + 120_000_000_000,
                               MAP_RTP + 120 * SAMPLE_RATE)
    with caplog.at_level(logging.WARNING):
        sr.revalidate_radiod_pair()

    writer.restore_timing_mapping.assert_called_once_with(MAP_GPS_NS, MAP_RTP)
    assert writer.add_timing_snapshot.call_count == 1
    sr.ring_buffer.update_anchor.assert_called_once_with(
        gps_time_ns=MAP_GPS_NS + round(saved_offset_ns), rtp_timesnap=MAP_RTP)
    assert sr._ring_anchor_state == (MAP_GPS_NS, MAP_RTP, saved_offset_ns)
    assert sr._prev_mapping is None
    assert any(r.levelno == logging.ERROR and 'ANCHOR ROUND TRIP' in r.getMessage()
               for r in caplog.records)


def test_core_t6_reference_only_while_authoritative():
    from types import SimpleNamespace
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    from hf_timestd.core.t6_anchor_authority import T6AuthorityState

    cmap, ref = _t6_reference_on_mapping()
    core = CoreRecorderV2.__new__(CoreRecorderV2)
    core._t6_native_anchor = ref.anchor
    core._t6_counter_map = cmap
    core._t6_authority_last_decision = None
    assert core._t6_pair_step_reference() is None
    for state in T6AuthorityState:
        core._t6_authority_last_decision = SimpleNamespace(state=state)
        got = core._t6_pair_step_reference()
        if state is T6AuthorityState.AUTHORITATIVE:
            assert got == T6Reference(ref.anchor, cmap)
        else:
            assert got is None
    # Never raises, even on a malformed decision.
    core._t6_authority_last_decision = object()
    assert core._t6_pair_step_reference() is None


# ---------------------------------------------------------------------------
# Task 4: field probe, the legacy fields torn against the atomic pair.
# ---------------------------------------------------------------------------
def test_probe_logs_and_counts_torn_legacy_fields(caplog):
    sr = _recorder()
    writer = MagicMock()
    writer.evaluate_pair.side_effect = lambda g, r: pair_disagreement_s(
        g, r, REF_GPS_NS, REF_RTP, SAMPLE_RATE)
    sr.archive_writer = writer
    sr.channel_info = _torn_ci()

    with caplog.at_level(logging.WARNING):
        sr.revalidate_radiod_pair()
        sr.revalidate_radiod_pair()

    probes = [r for r in caplog.records if 'TORN-READ PROBE' in r.getMessage()]
    assert len(probes) == 1
    assert probes[0].levelno == logging.WARNING
    assert 'implied_diff=+1.000 s' in probes[0].getMessage()
    assert sr.torn_read_probe_count == 2
    writer.add_timing_snapshot.assert_not_called()


def test_probe_silent_when_legacy_fields_agree(caplog):
    sr = _recorder()
    writer = MagicMock()
    writer.evaluate_pair.return_value = 0.0
    sr.archive_writer = writer
    sr.channel_info = _pair_ci(REF_GPS_NS, REF_RTP)
    with caplog.at_level(logging.WARNING):
        sr.revalidate_radiod_pair()
    assert not any('TORN-READ PROBE' in r.getMessage() for r in caplog.records)
    assert sr.torn_read_probe_count == 0
