"""The radiod pilot listener: row selection, address parsing, the loop.

No live network: the loop runs over a fake socket and a list of packets.
"""
import io
import json
import socket
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from radiod_pilot import listen, tlv  # noqa: E402


def _uint(t, x):
    b = x.to_bytes(8, "big").lstrip(b"\0")
    return bytes([t, len(b)]) + b


def _int64(t, x):
    return _uint(t, x & (2**64 - 1))


def _packet(ssrc, epoch=5, anchor_rtp=1000, anchor_in=1350000, pilot=False):
    p = bytes([0])
    p += _uint(tlv.T_OUTPUT_SSRC, ssrc)
    p += _int64(tlv.T_GPS_TIME, 1478000000123456789)
    p += _uint(tlv.T_RTP_TIMESNAP, 2000)
    p += _int64(tlv.T_INPUT_SAMPLES, 99999999)
    p += _uint(tlv.T_INPUT_SAMPRATE, 129600000)
    p += _uint(tlv.T_OUTPUT_SAMPRATE, 96000)
    p += _int64(tlv.T_INPUT_EPOCH, epoch)
    p += _uint(tlv.T_ANCHOR_RTP, anchor_rtp)
    p += _int64(tlv.T_ANCHOR_INPUT_SAMPLE, anchor_in)
    if pilot:
        p += _int64(tlv.T_PILOT_EDGE_SAMPLE, 77777)
        p += _uint(tlv.T_PILOT_LOCK, 1)
    return p + bytes([0])


def test_decoder_reads_input_samprate():
    assert tlv.decode(_packet(1))["input_samprate"] == 129600000


# --- row selection ----------------------------------------------------

def test_pilot_rows_every_packet():
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[], anchor_every_s=10.0)
    d = tlv.decode(_packet(45375, pilot=True))
    r1 = f.select(dict(d), now=100.0)
    r2 = f.select(dict(d), now=100.1)
    assert r1["kind"] == "pilot" and r2["kind"] == "pilot"
    assert r1["logged_at_unix"] == 100.0
    for k in ("ssrc", "input_epoch", "anchor_rtp", "anchor_input_sample",
              "pilot_edge_sample", "pilot_lock", "gps_time", "rtp_timesnap",
              "input_samples"):
        assert k in r1


def test_other_ssrc_ignored():
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[7], anchor_every_s=1.0)
    assert f.select(tlv.decode(_packet(8)), now=1.0) is None
    assert f.select(tlv.decode(_packet(8, pilot=True)), now=1.0) is None


def test_anchor_rows_throttled_per_ssrc():
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[7, 9], anchor_every_s=10.0)
    assert f.select(tlv.decode(_packet(7)), now=100.0)["kind"] == "anchor"
    assert f.select(tlv.decode(_packet(7)), now=105.0) is None
    assert f.select(tlv.decode(_packet(9)), now=105.0)["kind"] == "anchor"
    assert f.select(tlv.decode(_packet(7)), now=110.0)["kind"] == "anchor"


def test_anchor_row_written_at_once_on_epoch_change():
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[7], anchor_every_s=10.0)
    assert f.select(tlv.decode(_packet(7, epoch=5)), now=100.0)
    assert f.select(tlv.decode(_packet(7, epoch=6)), now=101.0)["input_epoch"] == 6


def test_anchor_row_written_at_once_when_tie_moves():
    # A block the demod thread skipped moves the input index against RTP:
    # the RTP-to-input offset changes inside one epoch.  Log it at once.
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[7], anchor_every_s=10.0)
    assert f.select(tlv.decode(_packet(7, anchor_rtp=1000, anchor_in=1350000)), now=100.0)
    # same tie, later anchor: 10 output samples on = 13500 input samples on
    assert f.select(tlv.decode(_packet(7, anchor_rtp=1010, anchor_in=1363500)), now=101.0) is None
    r = f.select(tlv.decode(_packet(7, anchor_rtp=1020, anchor_in=1378350)), now=102.0)
    assert r is not None and r["kind"] == "anchor"


def test_pilot_ssrc_without_report_logs_as_anchor():
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[], anchor_every_s=10.0)
    r = f.select(tlv.decode(_packet(45375)), now=1.0)
    assert r["kind"] == "anchor"


def test_packet_without_anchor_not_logged_as_anchor():
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[7], anchor_every_s=1.0)
    bare = bytes([0]) + _uint(tlv.T_OUTPUT_SSRC, 7) + bytes([0])
    assert f.select(tlv.decode(bare), now=1.0) is None


# --- addresses ----------------------------------------------------------

def _resolver(table):
    def r(host):
        if host in table:
            return table[host]
        raise socket.gaierror("no such host")
    return r


def test_parse_status_name_and_port():
    res = _resolver({"ac0g-b1-a-status.local": "239.1.2.3"})
    assert listen.parse_status("ac0g-b1-a-status.local:5006", res) == ("239.1.2.3", 5006)
    assert listen.parse_status("ac0g-b1-a-status.local", res) == ("239.1.2.3", 5006)
    assert listen.parse_status("239.9.9.9:6000", res) == ("239.9.9.9", 6000)


@pytest.mark.parametrize("bad", ["", ":5006", "239.1.2.3:x", "239.1.2.3:70000",
                                 "nosuch.local:5006", "10.0.0.1:5006"])
def test_parse_status_bad_raises_valueerror(bad):
    with pytest.raises(ValueError):
        listen.parse_status(bad, _resolver({"10.0.0.1": "10.0.0.1"}))


def test_main_bad_status_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as e:
        listen.main(["--status", "239.1.2.3:notaport", "--ssrc", "1", "--out", "/dev/null"])
    assert e.value.code == 2
    assert "--status" in capsys.readouterr().err


def test_membership_request_default_and_address():
    assert listen.membership_request("239.1.2.3", None) == \
        socket.inet_aton("239.1.2.3") + socket.inet_aton("0.0.0.0")
    assert listen.membership_request("239.1.2.3", "192.168.1.60") == \
        socket.inet_aton("239.1.2.3") + socket.inet_aton("192.168.1.60")


def test_membership_request_interface_name():
    m = listen.membership_request("239.1.2.3", "lo")
    assert m == struct.pack("4s4si", socket.inet_aton("239.1.2.3"),
                            socket.inet_aton("0.0.0.0"), socket.if_nametoindex("lo"))


def test_membership_request_unknown_interface():
    with pytest.raises(ValueError):
        listen.membership_request("239.1.2.3", "nosuchif0")


# --- the loop -----------------------------------------------------------

class FakeSock:
    def __init__(self, items):
        self.items = list(items)

    def recvmsg(self, n, ancn):
        if not self.items:
            raise KeyboardInterrupt
        x = self.items.pop(0)
        if isinstance(x, BaseException):
            raise x
        # in_pktinfo: ifindex, spec_dst, header destination address
        info = struct.pack("i4s4s", 2, socket.inet_aton("192.168.1.60"),
                           socket.inet_aton("239.55.69.43"))
        return x, [(socket.IPPROTO_IP, listen.IP_PKTINFO, info)], 0, ("192.168.1.165", 5006)


def test_run_survives_socket_errors_and_junk():
    sock = FakeSock([
        OSError(100, "Network is down"),
        socket.timeout(),
        b"\x01junk",
        _packet(45375, pilot=True),
        OSError(100, "Network is down"),
        _packet(45375, pilot=True),
    ])
    out, err = io.StringIO(), io.StringIO()
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[], anchor_every_s=1.0)
    t = iter(range(1000))
    n = listen.run(sock, out, f, now_fn=lambda: float(next(t)), err=err, sleep_fn=lambda s: None)
    rows = [json.loads(x) for x in out.getvalue().splitlines()]
    assert n == 2 and len(rows) == 2
    assert all(r["kind"] == "pilot" for r in rows)
    assert all(r["group"] == "239.55.69.43" for r in rows)
    assert "Network is down" in err.getvalue()


class BrokenFile:
    def __init__(self):
        self.calls = 0

    def write(self, s):
        self.calls += 1
        if self.calls == 1:
            raise OSError(28, "No space left on device")
        self.last = s

    def flush(self):
        pass


def test_run_survives_write_errors():
    sock = FakeSock([_packet(45375, pilot=True), _packet(45375, pilot=True)])
    out, err = BrokenFile(), io.StringIO()
    f = listen.RowFilter(pilot_ssrc=45375, anchor_ssrcs=[], anchor_every_s=1.0)
    n = listen.run(sock, out, f, now_fn=lambda: 1.0, err=err, sleep_fn=lambda s: None)
    assert n == 1 and "No space left" in err.getvalue()
    assert json.loads(out.last)["kind"] == "pilot"


def test_main_rejects_mixed_ports(capsys):
    with pytest.raises(SystemExit) as e:
        listen.main(["--status", "239.1.2.3:5006", "--status", "239.1.2.4:5007",
                     "--ssrc", "1", "--out", "/dev/null"])
    assert e.value.code == 2 and "one port" in capsys.readouterr().err


class OptSock:
    """Records setsockopt calls; refuses the options named in ``refuse``."""
    def __init__(self, refuse=()):
        self.refuse, self.opts = set(refuse), []

    def setsockopt(self, level, opt, val):
        if opt in self.refuse:
            raise OSError(92, "Protocol not available")
        self.opts.append((level, opt, val))

    def bind(self, addr):
        pass

    def settimeout(self, t):
        pass


def test_open_socket_sets_multicast_all_off_and_pktinfo():
    s = OptSock()
    listen.open_socket(["239.1.2.3"], 5006, None, sock_factory=lambda: s, err=io.StringIO())
    assert (socket.IPPROTO_IP, listen.IP_MULTICAST_ALL, 0) in s.opts
    assert (socket.IPPROTO_IP, listen.IP_PKTINFO, 1) in s.opts


def test_open_socket_refuses_without_multicast_all_off():
    with pytest.raises(OSError, match="IP_MULTICAST_ALL"):
        listen.open_socket(["239.1.2.3"], 5006, None,
                           sock_factory=lambda: OptSock({listen.IP_MULTICAST_ALL}),
                           err=io.StringIO())


def test_open_socket_warns_without_pktinfo():
    err = io.StringIO()
    listen.open_socket(["239.1.2.3"], 5006, None,
                       sock_factory=lambda: OptSock({listen.IP_PKTINFO}), err=err)
    assert "WARNING" in err.getvalue() and "IP_PKTINFO" in err.getvalue()
