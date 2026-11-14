"""Byte sources for the GNSS VTEC reader: TCP stream or local USB serial port.

``live_vtec.py`` reads bytes from a source and hands them to the UBX or RTCM3
parser.  A source has four calls:

    open()   connect; raises on failure
    read()   bytes, or None when the wait ended with no data, or b"" when the
             peer closed.  A serial fault raises OSError.
    close()  idempotent
    slice_s  how long one read() may wait, in seconds

Every wait ends well inside the unit's WatchdogSec=60.
"""
import logging
import socket
import time
from typing import Callable, Optional

logger = logging.getLogger("live_vtec")

# CFG-VALSET layer bit for RAM.  BBR (2) and flash (4) are never set: the
# receiver forgets this change on power-off, so we never alter the owner's
# stored configuration.
LAYER_RAM = 0x01

RECEIVER_CONFIG_KEYS = [
    ("CFG_USBOUTPROT_UBX", 1),
    ("CFG_MSGOUT_UBX_RXM_RAWX_USB", 1),
    ("CFG_MSGOUT_UBX_RXM_SFRBX_USB", 1),
]

# UBX-ACK-ACK / UBX-ACK-NAK for CFG-VALSET (class 0x06, id 0x8A): the frame is
# b5 62 05 01|00 02 00 06 8a ck_a ck_b.
_ACK_HEAD = bytes.fromhex("b56205010200068a")
_NAK_HEAD = bytes.fromhex("b56205000200068a")
_ACK_LEN = 10


def build_receiver_config() -> bytes:
    """The one UBX-CFG-VALSET we send: RAM layer only, names from pyubx2."""
    from pyubx2 import UBXMessage
    return UBXMessage.config_set(LAYER_RAM, 0, RECEIVER_CONFIG_KEYS).serialize()


class TcpSource:
    """TCP stream, e.g. a ZED-F9P behind ser2net.  Never writes."""
    CONNECT_TIMEOUT_S = 10.0
    READ_TIMEOUT_S = 20.0

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.slice_s = self.READ_TIMEOUT_S
        self.config_result = None
        self._sock: Optional[socket.socket] = None

    def describe(self) -> str:
        return f"{self.host}:{self.port}"

    def open(self) -> None:
        self.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # ⛔ Every blocking wait must end well inside WatchdogSec=60
            # (AC0G-ND 2026-10-04: a 60 s recv timeout got the process killed).
            sock.settimeout(self.CONNECT_TIMEOUT_S)
            sock.connect((self.host, self.port))
            sock.settimeout(self.READ_TIMEOUT_S)
        except BaseException:
            sock.close()
            raise
        self._sock = sock

    def read(self) -> Optional[bytes]:
        try:
            return self._sock.recv(4096)
        except socket.timeout:
            return None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


class SerialSource:
    """Local USB serial port (u-blox CDC-ACM).  Writes only the VALSET."""
    READ_TIMEOUT_S = 2.0
    ACK_WAIT_S = 5.0
    WRITE_TIMEOUT_S = 5.0

    def __init__(self, device: str, configure_receiver: bool = True,
                 opener: Optional[Callable] = None,
                 ack_wait_s: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.device = device
        self.configure_receiver = configure_receiver
        self.slice_s = self.READ_TIMEOUT_S
        self.config_result: Optional[str] = None   # "ack" | "nak" | "none"
        self._opener = opener or self._open_port
        self._ack_wait_s = self.ACK_WAIT_S if ack_wait_s is None else ack_wait_s
        self._clock = clock
        self._port = None
        self._pending = b""

    def describe(self) -> str:
        return self.device

    def _open_port(self):
        import serial
        # pyserial puts a tty in raw mode (echo, canonical mode off), so the
        # receiver never hears its own output.  Baud means nothing on USB CDC.
        return serial.Serial(self.device, 115200,
                             timeout=self.READ_TIMEOUT_S,
                             write_timeout=self.WRITE_TIMEOUT_S,
                             exclusive=True)

    def open(self) -> None:
        self.close()
        self._port = self._opener()
        self._pending = b""
        self.config_result = None
        if self.configure_receiver:
            try:
                self._send_receiver_config()
            except OSError as e:
                self.close()
                raise
            except Exception as e:      # a config problem never stops the reader
                logger.warning(f"Receiver configuration failed: {e}")

    def _send_receiver_config(self) -> None:
        """⛔ The ONLY bytes this program writes to the receiver.

        One UBX-CFG-VALSET to the RAM layer: UBX on the USB port, RXM-RAWX and
        RXM-SFRBX at rate 1.  Re-sent at every open, because RAM does not
        survive a receiver power cycle.  Waits a bounded time for ACK/NAK; a
        NAK or silence logs a warning and the reader carries on."""
        self._port.write(build_receiver_config())
        self._port.flush()
        buf = b""
        deadline = self._clock() + self._ack_wait_s
        while self._clock() < deadline:
            chunk = self._port.read(256)
            if chunk:
                buf += chunk
            for head, result in ((_ACK_HEAD, "ack"), (_NAK_HEAD, "nak")):
                i = buf.find(head)
                if i >= 0 and len(buf) >= i + _ACK_LEN:
                    self._pending = buf[i + _ACK_LEN:]
                    self.config_result = result
                    if result == "ack":
                        logger.info(f"{self.device}: receiver ACKed RAWX/SFRBX "
                                    "configuration (RAM layer)")
                    else:
                        logger.warning(f"{self.device}: receiver NAKed the "
                                       "RAWX/SFRBX configuration; carrying on")
                    return
        self.config_result = "none"
        logger.warning(f"{self.device}: no answer to the RAWX/SFRBX configuration "
                       f"within {self._ack_wait_s:.0f}s; carrying on")

    def read(self) -> Optional[bytes]:
        if self._pending:
            data, self._pending = self._pending, b""
            return data
        waiting = getattr(self._port, "in_waiting", 0) or 0
        data = self._port.read(min(4096, max(1, waiting)))
        return data if data else None

    def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None


def make_source(gnss_cfg: dict, host: Optional[str] = None,
                port: Optional[int] = None, device: Optional[str] = None):
    """Serial when a device is given (CLI or config), else TCP as before."""
    device = device or gnss_cfg.get("device")
    if device:
        return SerialSource(device, bool(gnss_cfg.get("configure_receiver", True)))
    return TcpSource(host or gnss_cfg.get("host", "192.168.0.202"),
                     port or gnss_cfg.get("port", 2001))
