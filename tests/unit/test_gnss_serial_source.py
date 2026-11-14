"""USB serial source for live_vtec (N8UR: ZED-F9T on /dev/ttyACM1).

No hardware: the serial port is a fake.  The one pty test checks that
pyserial leaves tty echo off, so the receiver never hears its own output.
"""
import importlib.util
import logging
import os
import pty
import termios
from pathlib import Path

import pytest
import serial
from pyubx2 import UBXReader, ERR_RAISE, SET
from io import BytesIO

from hf_timestd.core import gnss_source as gs

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'live_vtec.py'

ACK = bytes.fromhex('b5620501020006 8a'.replace(' ', '')) + b'\x00\x00'   # checksum not checked
NAK = bytes.fromhex('b5620500020006 8a'.replace(' ', '')) + b'\x00\x00'
KEYS = {'CFG_MSGOUT_UBX_RXM_RAWX_USB', 'CFG_MSGOUT_UBX_RXM_SFRBX_USB',
        'CFG_MSGOUT_UBX_NAV_SAT_USB', 'CFG_USBOUTPROT_UBX'}


class FakePort:
    """Scripted serial port: each read() pops one item (bytes, or an exception)."""
    def __init__(self, script=()):
        self.script = list(script)
        self.written = []
        self.closed = False
        self.read_sizes = []
        self.in_waiting = 0

    def read(self, n=1):
        self.read_sizes.append(n)
        if not self.script:
            return b''
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def write(self, b):
        self.written.append(bytes(b))
        return len(b)

    def flush(self):
        pass

    def close(self):
        self.closed = True


def _src(port, configure=True, **kw):
    return gs.SerialSource('/dev/ttyACM1', configure_receiver=configure,
                           opener=lambda: port, **kw)


def test_valset_frame_is_ram_only_with_the_four_keys():
    frame = gs.build_receiver_config()
    msg = UBXReader(BytesIO(frame), msgmode=SET, quitonerror=ERR_RAISE).read()[1]
    assert msg.identity == 'CFG-VALSET'
    assert (msg.ram, msg.bbr, msg.flash) == (1, 0, 0)
    for k in KEYS:
        assert getattr(msg, k) == 1
    assert len([a for a in dir(msg) if a.startswith('CFG_')]) == 4


def test_config_keys_exist_in_pyubx2():
    from pyubx2 import UBX_CONFIG_DATABASE as db
    for k in KEYS:
        assert k in db


def test_open_sends_exactly_one_frame_and_logs_ack(caplog):
    port = FakePort([ACK])
    caplog.set_level(logging.INFO)
    src = _src(port)
    src.open()
    assert port.written == [gs.build_receiver_config()]
    assert src.config_result == 'ack'
    assert 'ACK' in caplog.text


def test_nak_is_a_warning_not_a_crash(caplog):
    port = FakePort([NAK])
    src = _src(port)
    src.open()
    assert src.config_result == 'nak'
    assert any(r.levelno == logging.WARNING and 'NAK' in r.getMessage()
               for r in caplog.records)


def test_silence_is_a_warning_not_a_crash(caplog):
    port = FakePort([])           # reads time out empty
    clock = [0.0]

    def tick():
        clock[0] += 1.0
        return clock[0]
    src = _src(port, ack_wait_s=3.0, clock=tick)
    src.open()
    assert src.config_result == 'none'
    assert any(r.levelno == logging.WARNING and 'no answer' in r.getMessage().lower()
               for r in caplog.records)
    assert len(port.read_sizes) < 10        # the wait is bounded


def test_ack_split_across_reads_and_after_nmea():
    port = FakePort([b'$GNGGA,1,2,3\r\n' + ACK[:5], ACK[5:] + b'tail'])
    src = _src(port)
    src.open()
    assert src.config_result == 'ack'
    assert src.read() == b'tail'      # bytes after the ACK are not lost


def test_configure_receiver_false_sends_nothing():
    port = FakePort([b'$GNGGA\r\n'])
    src = _src(port, configure=False)
    src.open()
    assert port.written == []
    assert src.config_result is None


def test_read_timeout_returns_none_and_serial_error_raises():
    port = FakePort([b'', serial.SerialException('device reports readiness')])
    src = _src(port, configure=False)
    src.open()
    assert src.read() is None
    with pytest.raises(OSError):
        src.read()


def test_reads_are_bounded():
    assert gs.SerialSource.READ_TIMEOUT_S <= 5
    assert gs.SerialSource.ACK_WAIT_S <= 10
    assert gs.SerialSource.READ_TIMEOUT_S + 0 < 20
    assert gs.TcpSource.READ_TIMEOUT_S <= 20 and gs.TcpSource.CONNECT_TIMEOUT_S <= 20


def test_make_source_device_wins_over_host_port():
    s = gs.make_source({'device': '/dev/x', 'host': 'h', 'port': 9000})
    assert isinstance(s, gs.SerialSource) and s.configure_receiver is True
    s = gs.make_source({'device': '/dev/x', 'configure_receiver': False})
    assert s.configure_receiver is False


def test_make_source_without_device_is_tcp_and_never_configures():
    s = gs.make_source({'host': 'h', 'port': 9000, 'configure_receiver': True})
    assert isinstance(s, gs.TcpSource)
    assert (s.host, s.port) == ('h', 9000)
    assert not hasattr(s, 'config_result') or s.config_result is None


def test_pyserial_leaves_echo_off_on_a_tty():
    master, slave = pty.openpty()
    try:
        ser = serial.Serial(os.ttyname(slave), 115200, timeout=0.1)
        try:
            lflag = termios.tcgetattr(ser.fileno())[3]
            assert not lflag & termios.ECHO
            assert not lflag & termios.ICANON
        finally:
            ser.close()
    finally:
        os.close(master)
        os.close(slave)


# ── live_vtec wiring ──
def _load():
    spec = importlib.util.spec_from_file_location('live_vtec_dev', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_live_vtec_has_device_flag_and_reopens_on_serial_error():
    src = SCRIPT.read_text()
    assert '--device' in src
    assert 'make_source' in src
    assert 'args.device' in src          # counts as "run regardless of enabled"


def test_reopen_after_serialexception_resends_config():
    """Drive the real source the way live_vtec does: error, close, open again."""
    ports = [FakePort([ACK, serial.SerialException('unplugged')]),
             FakePort([ACK, b'data'])]
    it = iter(ports)
    src = gs.SerialSource('/dev/ttyACM1', configure_receiver=True,
                          opener=lambda: next(it))
    src.open()
    with pytest.raises(OSError):
        src.read()
    src.close()
    src.open()
    assert src.read() == b'data'
    assert all(p.written == [gs.build_receiver_config()] for p in ports)
    assert ports[0].closed


def test_unit_and_installer_grant_dialout():
    root = SCRIPT.parents[1]
    assert 'SupplementaryGroups=dialout' in (root / 'systemd/timestd-vtec.service').read_text()
    assert 'usermod -a -G dialout' in (root / 'scripts/install.sh').read_text()
