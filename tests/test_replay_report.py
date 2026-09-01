"""summarise()/render() must carry the per-channel-minute layer, not just

per-station state counts.  I2: channel_state and unclaimed_ms are computed
by the cascade on every minute and were being thrown away before they
reached ReplayReport."""
from hf_timestd.core.admission_cascade import (
    AdmissionState, ChannelState, ChannelVerdict, StationVerdict,
)
from hf_timestd.replay.report import summarise
from hf_timestd.replay.runner import MinuteVerdict


def _mv(minute_utc, *, admitted=(), unclaimed_ms=(), channel_state,
        deployed_labels=frozenset()):
    stations = {}
    for station in ("WWV", "WWVH", "BPM"):
        if station in admitted:
            stations[station] = StationVerdict(
                station, AdmissionState.ADMITTED, 4.0, "")
        else:
            stations[station] = StationVerdict(
                station, AdmissionState.BELOW_FLOOR, None, "nothing above floor")
    verdict = ChannelVerdict(stations=stations, channel_state=channel_state,
                             unclaimed_ms=list(unclaimed_ms))
    return MinuteVerdict(channel="SHARED_10000", minute_utc=minute_utc,
                         verdict=verdict, deployed_labels=set(deployed_labels))


def test_summarise_counts_channel_states():
    verdicts = [
        _mv(0, admitted=["WWV"], channel_state=ChannelState.CHANNEL_PARTIAL),
        _mv(60, channel_state=ChannelState.CHANNEL_SILENT),
        _mv(120, unclaimed_ms=[13.0], channel_state=ChannelState.CHANNEL_UNIDENTIFIED),
    ]
    report = summarise(verdicts)

    assert report.channel_states[ChannelState.CHANNEL_PARTIAL] == 1
    assert report.channel_states[ChannelState.CHANNEL_SILENT] == 1
    assert report.channel_states[ChannelState.CHANNEL_UNIDENTIFIED] == 1
    assert report.unclaimed_arrivals == 1


def test_render_prints_channel_states_section():
    verdicts = [
        _mv(120, unclaimed_ms=[13.0], channel_state=ChannelState.CHANNEL_UNIDENTIFIED),
    ]
    text = summarise(verdicts).render()

    assert "channel states:" in text
    assert "CHANNEL_UNIDENTIFIED" in text
    assert "unclaimed arrivals: 1" in text


def test_render_prints_every_admission_state_with_explicit_zero():
    """A state that never fired must read as zero, not vanish from the
    report -- most_common() over a Counter silently drops it."""
    verdicts = [_mv(0, admitted=["WWV"], channel_state=ChannelState.CHANNEL_PARTIAL)]
    text = summarise(verdicts).render()

    for state in AdmissionState:
        assert state.value in text, f"{state.value} missing from render()"
    assert "OFF_MODEL" in text
    assert "INCONSISTENT" in text
