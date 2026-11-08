"""label_record's pure labelling, over synthetic status and RTP packets.

The synthetic station: an input counter at 129.6 Msps whose sample
E_TRUE carries the TS-1 edge of UTC second N0 at the ADC.  Every channel
filter delays content by F = (M - 1) / 2 input samples, so radiod reports
the raw edge at E_TRUE + F, and output sample r of a channel holds the
content of input index in(r) - F.  A sample's label must therefore be
N0 + (in(r) - F - E_TRUE) / fs_in, whatever the channel's rate.
"""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from radiod_pilot import label_record as lr  # noqa: E402
from radiod_pilot import tlv  # noqa: E402

IN_RATE = 129_600_000
FIR = 648_001                      # B1's FILTER_FIR_LENGTH
F = (FIR - 1) // 2                 # 324 000 input samples = 2.5 ms
N0 = 1_794_180_000                 # a UTC second (unix)
E_TRUE = 300_000_000_123           # input index whose content is the edge
FRAC = 0.4375
EPOCH = 1_478_213_294_638_026_024
GPS = lr.GPS_TO_UNIX_NS


# ---- a minimal status encoder (status.c conventions) ----

def _int(t, v):
    b = v.to_bytes(8, "big").lstrip(b"\0") if v else b""
    return bytes([t, len(b)]) + b


def _sint(t, v):
    return _int(t, v & 0xFFFFFFFFFFFFFFFF)


def _dbl(t, v):
    return _int(t, int.from_bytes(struct.pack(">d", v), "big"))


def status_packet(**kv):
    out = b"\0"
    enc = {
        "ssrc": (18, _int), "input_samprate": (10, _int), "output_samprate": (20, _int),
        "input_epoch": (240, _int), "anchor_rtp": (239, _int),
        "anchor_input_sample": (238, _sint), "filter_fir_length": (43, _int),
        "filter_blocksize": (42, _int), "output_encoding": (107, _int),
        "output_channels": (49, _int), "gps_time": (3, _sint), "rtp_timesnap": (8, _int),
        "pilot_edge_sample": (237, _sint), "pilot_edge_frac": (236, _dbl),
        "pilot_edge_utc": (235, _sint), "pilot_lock": (231, _int),
        "pilot_filter_delay": (229, _dbl),
    }
    for k, v in kv.items():
        if k == "data_dest":
            g, p = v
            out += bytes([17, 6]) + bytes(int(x) for x in g.split(".")) + p.to_bytes(2, "big")
            continue
        t, fn = enc[k]
        out += fn(t, int(v) if k != "pilot_edge_frac" and k != "pilot_filter_delay" else v)
    return out + b"\0"


def chan_status(out_rate, a_in, a_rtp, **extra):
    return tlv.decode(status_packet(
        ssrc=1234, input_samprate=IN_RATE, output_samprate=out_rate, input_epoch=EPOCH,
        anchor_rtp=a_rtp, anchor_input_sample=a_in, filter_fir_length=FIR,
        filter_blocksize=2_592_000, output_encoding=4, output_channels=2,
        data_dest=("239.1.2.3", 5004), **extra))


def pilot_report(edge_true=E_TRUE, frac=FRAC, n=N0, lock=True, epoch=EPOCH, f_s=0.0025):
    return tlv.decode(status_packet(
        ssrc=45375, input_samprate=IN_RATE, input_epoch=epoch,
        pilot_edge_sample=edge_true + F, pilot_edge_frac=frac,
        pilot_edge_utc=n * 10**9 - GPS, pilot_lock=1 if lock else 0, pilot_filter_delay=f_s))


def true_offset_ns(input_index_radiod, frac=FRAC):
    """Ground truth, ns after N0: content index minus the edge's content index.
    (Offsets, not absolute ns: a float at 1.8e18 ns resolves only 256 ns.)"""
    return ((input_index_radiod - F) - (E_TRUE + frac)) * 1e9 / IN_RATE


def off(label):
    whole, frac = label
    return (whole - N0 * 10**9) + frac


# ---- tests ----

def test_decoder_reads_the_new_status_fields():
    d = chan_status(24_000, 10, 20)
    assert d["filter_fir_length"] == FIR
    assert d["output_encoding"] == 4 and d["output_channels"] == 2
    assert d["data_dest"] == ("239.1.2.3", 5004)
    assert d["filter_blocksize"] == 2_592_000


@pytest.mark.parametrize("out_rate", [96_000, 24_000, 4_000])
def test_label_matches_truth_for_any_rate(out_rate):
    decim = IN_RATE // out_rate
    a_in = E_TRUE + 7 * IN_RATE + 5 * decim          # some block, 7 s after the edge
    a_rtp = 0xFFFFFF00                                # near the wrap
    lab = lr.Labeller.from_status(chan_status(out_rate, a_in, a_rtp), pilot_report())
    for k in (-500_000, -1, 0, 1, 12_345, 3 * out_rate):
        assert off(lab.utc_ns(k)) == pytest.approx(true_offset_ns(a_in + k * decim), abs=1e-3)


def test_filter_delays_are_both_taken_out():
    """A pilot channel with a different filter delay than the recorded channel."""
    out_rate, decim = 96_000, 1350
    a_in = E_TRUE + 3 * IN_RATE
    f_p = 0.001                              # pilot: 1 ms, channel: 2.5 ms
    rep = pilot_report()
    rep["pilot_edge_sample"] = E_TRUE + round(f_p * IN_RATE)
    lab = lr.Labeller.from_status(chan_status(out_rate, a_in, 0), {**rep, "pilot_filter_delay": f_p})
    assert off(lab.utc_ns(0)) == pytest.approx(true_offset_ns(a_in), abs=1e-3)
    assert lab.f_chan - lab.f_pilot == pytest.approx(F - f_p * IN_RATE)


def test_edge_sample_of_the_named_second_labels_it_exactly():
    """The output sample sitting on the raw edge reads the named second (+chain)."""
    decim = 1350
    edge_raw = E_TRUE + F
    a_in = edge_raw - 100 * decim                       # 100 outputs before the edge
    rep = pilot_report(frac=0.0)
    lab = lr.Labeller.from_status(chan_status(96_000, a_in, 77), rep, chain_delay_ns=200)
    assert lab.utc_ns(100) == (N0 * 10**9 + 200, 0.0)


def test_epoch_mismatch_refused():
    with pytest.raises(ValueError, match="epoch"):
        lr.Labeller.from_status(chan_status(24_000, 0, 0), pilot_report(epoch=EPOCH + 1))


def test_select_report_takes_latest_locked_of_epoch():
    a = pilot_report(n=N0)
    b = pilot_report(n=N0 + 30)
    c = pilot_report(n=N0 + 60, lock=False)
    d = pilot_report(n=N0 + 90, epoch=EPOCH + 1)
    assert lr.select_report([a, b, c, d], EPOCH) is b
    assert lr.select_report([c, d], EPOCH) is None


def test_first_index_at_or_after_is_the_boundary():
    out_rate, decim = 24_000, 5400
    a_in = E_TRUE + 2 * IN_RATE + 1234
    lab = lr.Labeller.from_status(chan_status(out_rate, a_in, 5), pilot_report())
    t = (N0 + 5) * 10**9
    k = lab.first_index_at_or_after(t)
    t_off = t - N0 * 10**9
    assert off(lab.utc_ns(k)) >= t_off > off(lab.utc_ns(k - 1))
    assert off(lab.utc_ns(k)) - t_off < 1e9 / out_rate


def test_anchors_agree_and_disagree():
    decim = 5400
    a = [(EPOCH, 100, 10_000_000), (EPOCH, 100 + 480, 10_000_000 + 480 * decim)]
    assert lr.anchors_agree(a, decim)
    assert not lr.anchors_agree(a + [(EPOCH, 1060, 10_000_000 + 961 * decim)], decim)
    assert not lr.anchors_agree(a + [(EPOCH + 1, 1060, 10_000_000 + 960 * decim)], decim)


def rtp_packet(ssrc, seq, ts, payload, ext=False):
    b0 = 0x80 | (0x10 if ext else 0)
    hdr = struct.pack(">BBHII", b0, 0x60, seq, ts, ssrc)
    if ext:
        hdr += struct.pack(">HH", 0xBEDE, 1) + b"\0" * 4
    return hdr + payload


def test_parse_rtp_with_and_without_extension():
    p = rtp_packet(285821745, 7, 0xFFFFFFF0, b"abcdefgh")
    assert lr.parse_rtp(p) == (285821745, 7, 0xFFFFFFF0, b"abcdefgh")
    assert lr.parse_rtp(rtp_packet(1, 2, 3, b"xy" * 4, ext=True))[3] == b"xy" * 4
    assert lr.parse_rtp(b"\x00" * 20) is None


def frames(k, n):
    """n complex64 frames whose I carries the output index."""
    return b"".join(struct.pack("<ff", float(i), 0.0) for i in range(k, k + n))


def test_assemble_across_wrap_with_gap_and_duplicate():
    ref = 0xFFFFFF00
    pk = [((ref + k) & 0xFFFFFFFF, frames(k, 100)) for k in (0, 100, 300, 400)]
    pk.append(pk[1])                                       # duplicate
    first, buf, gaps = lr.assemble(list(reversed(pk)), ref)
    assert first == 0
    assert gaps == [(200, 100)]
    vals = [struct.unpack_from("<f", buf, 8 * i)[0] for i in range(500)]
    assert vals[:200] == list(range(200))
    assert vals[200:300] == [0.0] * 100
    assert vals[300:] == list(range(300, 500))


def test_cut_keeps_exactly_the_window_and_sidecar_names_first_sample():
    out_rate, decim = 24_000, 5400
    a_rtp = 0xFFFFF000
    a_in = E_TRUE + IN_RATE // 2 + 17 * decim          # half a second after the edge
    st = chan_status(out_rate, a_in, a_rtp, gps_time=(N0 + 1) * 10**9 - GPS + 15_000_000,
                     rtp_timesnap=(a_rtp + 480) & 0xFFFFFFFF)
    rep = pilot_report()
    lab = lr.Labeller.from_status(st, rep)
    # 4 s of packets from k = -24000 (before the window) on
    pk = [((a_rtp + k) & 0xFFFFFFFF, frames(k, 240)) for k in range(-24_000, 72_000, 240)]
    first, buf, gaps = lr.assemble(pk, a_rtp)
    start = (N0 + 1) * 10**9
    k0, data = lr.cut(first, buf, lab, start, 2)
    assert len(data) // 8 == 2 * out_rate
    assert struct.unpack_from("<f", data, 0)[0] == float(k0)
    side = lr.build_sidecar(chan_status=st, report=rep, lab=lab, k0=k0, n_frames=len(data) // 8,
                            start_unix_ns=start, seconds=2, anchors=[(EPOCH, a_rtp, a_in)],
                            gaps=gaps, n_packets=len(pk), data_dest=st["data_dest"],
                            reports_seen=1, report_spread_ns=None)
    fs = side["first_sample"]
    assert 0 <= fs["after_window_start_ns"] < 1e9 / out_rate
    assert off((fs["utc_unix_ns"], fs["utc_frac_ns"])) == pytest.approx(
        true_offset_ns(a_in + k0 * decim), abs=1e-3)
    assert fs["rtp"] == (a_rtp + k0) & 0xFFFFFFFF
    assert side["epoch"] == EPOCH and side["anchor"]["all_agree"]
    assert side["filter_delay"]["channel_input_samples"] == F
    assert side["filter_delay"]["net_input_samples"] == pytest.approx(0.0)
    assert side["gap_frames_in_window"] == 0
    # old pair: GPS_TIME read 15 ms late, labelling RTP_TIMESNAP's sample
    exp_old = (10**9 + 15_000_000 + (k0 - 480) * 1e9 / out_rate) - off(
        (fs["utc_unix_ns"], fs["utc_frac_ns"]))
    assert fs["old_pair_minus_label_ns"] == pytest.approx(exp_old, abs=1e-3)


def test_cut_refuses_a_short_recording():
    st = chan_status(24_000, E_TRUE + IN_RATE, 0)
    lab = lr.Labeller.from_status(st, pilot_report())
    first, buf, _ = lr.assemble([(0, frames(0, 24_000))], 0)
    with pytest.raises(ValueError, match="covers"):
        lr.cut(first, buf, lab, (N0 + 1) * 10**9, 2)


def test_parse_start():
    assert lr.parse_start("2026-11-08T23:40:00Z") == 1794181200 * 10**9
    with pytest.raises(ValueError):
        lr.parse_start("2026-11-08T23:40:00.5Z")
