"""The archive writer names the counter epoch and publishes the v2 state
record in its per-chunk timing block.  MEASUREMENT_MODEL §3: a radiod
restart renumbers the samples; no consumer extrapolates across it."""
from __future__ import annotations

from types import SimpleNamespace

from hamsci_dsp.timing_map import TimeMap
from hf_timestd.core.binary_archive_writer import BinaryArchiveWriter
from hf_timestd.core.time_map_producer import TimeMapInputs

T0 = 1_788_537_251_999_997_458


def _bare():
    w = BinaryArchiveWriter.__new__(BinaryArchiveWriter)
    w._counter_epoch_id = None
    w._counter_epoch_pair = None
    w.config = SimpleNamespace(sample_rate=96_000, channel_name="T6_96000")
    return w


def test_first_adoption_opens_an_epoch_named_by_the_pair():
    w = _bare()
    assert w.counter_epoch_id == "unregistered"
    assert w._note_counter_epoch(1_788_537_000_000_000_000, 5_000, 96_000) == "pair-1788537000000000000"


def test_a_pair_that_agrees_with_the_mapping_keeps_the_epoch():
    w = _bare()
    w._note_counter_epoch(1_788_537_000_000_000_000, 5_000, 96_000)
    # ten seconds later, 960,000 samples on, 3 ms of pair jitter: same counter
    assert w._note_counter_epoch(1_788_537_010_003_000_000, 965_000, 96_000) == "pair-1788537000000000000"


def test_a_pair_seconds_off_the_mapping_opens_a_new_epoch():
    w = _bare()
    w._note_counter_epoch(1_788_537_000_000_000_000, 5_000, 96_000)
    # radiod restarted: counter re-based near zero while UTC moved 60 s
    new = w._note_counter_epoch(1_788_537_060_000_000_000, 7, 96_000)
    assert new == "pair-1788537060000000000" and w.counter_epoch_id == new


class _Verdict(SimpleNamespace):
    pass


def _writer_with_provider(provider):
    w = _bare()
    w._gps_time_ns_raw = T0 - 12_000_000
    w._rtp_timesnap = 2_150_318_060
    w._note_counter_epoch(w._gps_time_ns_raw, w._rtp_timesnap, 96_000)
    w._time_map_provider = None
    w._time_map_counter_space = None
    w.set_time_map_provider(provider, counter_space="AC0G-B4-status.local/T6_96000")
    return w


def _verdict():
    return _Verdict(offset_ns=3_536_564.66, sigma_ns=982_495.95, tier="T6",
                    judge_age_s=0.3, segment_id=1, rate_ppm=None)


def test_timing_block_is_the_v2_state_record_with_legacy_keys_mirrored():
    seen = {}

    def provider(inputs: TimeMapInputs) -> TimeMap:
        seen["inputs"] = inputs
        from hamsci_dsp.timing_map import native_anchor_map
        return native_anchor_map(counter_space=inputs.counter_space,
                                 counter_epoch_id=inputs.counter_epoch_id,
                                 anchor_rtp=2_150_319_213, anchor_utc_ns=T0, sample_rate_hz=96_000,
                                 measured_at_utc_ns=T0, sigma_ns=4093, lock_credible=True,
                                 a_level="A1", a_level_provenance="observed",
                                 engineering=inputs.engineering)
    w = _writer_with_provider(provider)
    block = w._chunk_timing_block(_verdict(), chunk_boundary_utc_ns=T0 + 600 * 10**9)

    # v2 state record
    assert block["type"] == "state" and block["schema"] == "v2"
    assert block["origin"] == "native_anchor" and block["u_epoch_ns"] == 4093
    assert block["counter_epoch_id"].startswith("pair-")
    assert block["engineering"]["judge_tier"] == "T6"
    assert block["engineering"]["radiod_gps_time_ns"] == T0 - 12_000_000
    # legacy keys mirrored at top level for one release (Plan C retires the mirror)
    assert block["offset_ns"] == 3_536_564.66 and block["offset_sigma_ns"] == 982_495.95
    assert block["judge_tier"] == "T6" and block["radiod_rtp_timesnap"] == 2_150_318_060
    # the provider saw the writer's facts
    inp = seen["inputs"]
    assert inp.counter_space == "AC0G-B4-status.local/T6_96000"
    assert inp.gps_time_ns == T0 - 12_000_000 and inp.rtp_timesnap == 2_150_318_060
    assert inp.f_s_hz == 96_000 and inp.judge_tier == "T6"


def test_without_a_provider_the_legacy_block_is_unchanged():
    w = _bare()
    w._gps_time_ns_raw, w._rtp_timesnap = 1, 2
    w._time_map_provider = None
    block = w._chunk_timing_block(_verdict(), chunk_boundary_utc_ns=T0)
    assert set(block) == {"radiod_gps_time_ns", "radiod_rtp_timesnap", "offset_ns", "offset_sigma_ns",
                          "judge_tier", "judge_age_s", "segment_id", "rate_ppm"}


def test_no_verdict_and_no_provider_means_no_block():
    w = _bare()
    w._gps_time_ns_raw, w._rtp_timesnap = 1, 2
    w._time_map_provider = None
    assert w._chunk_timing_block(None, chunk_boundary_utc_ns=T0) is None


def test_a_provider_that_raises_yields_a_null_map_not_a_crash():
    def boom(inputs):
        raise RuntimeError("no authority")
    w = _writer_with_provider(boom)
    block = w._chunk_timing_block(_verdict(), chunk_boundary_utc_ns=T0)
    assert block["origin"] is None and "no authority" in block["reason"]
    assert block["offset_ns"] == 3_536_564.66      # legacy mirror still present
