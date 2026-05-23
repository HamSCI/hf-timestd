"""T5 disambiguation reference — Leo Bodnar LBE-1421 GPSDO NMEA over USB.

The LBE-1421 is the GPS-disciplined oscillator on bee1: it locks to GPS,
drives the 27 MHz reference into TS-1 (which generates the BPSK HF-PPS
injected into the RF feed), AND emits NMEA sentences over its USB-CDC
serial endpoint.  This module reads those NMEA sentences and exposes the
absolute GPS UTC of the most-recent PPS edge as the T5 disambiguation
reference for the BPSK PPS calibrator.

## Why this exists

Today the BPSK PPS calibrator's first-lock disambiguation walks the
T-level hierarchy and lands on T4 chronyc-tracking — meaning we use the
host system clock (disciplined by a LAN GPS NTP timeserver) to resolve
the integer-second ambiguity of the matched-filter edge.  That detour
inherits chrony's ~5 µs RMS discipline noise even though the LB-1421
GPSDO is sitting on the very same machine, GPS-locked to sub-100 ns,
emitting absolute UTC over USB.

The T5 path eliminates the detour:

  LB-1421 NMEA $GNRMC ──► host /dev/ttyACM3
                              │
                              ▼  (~100-500 ms USB latency)
                      Lb1421T5Probe
                              │
                              ▼  (integer-second precision)
              core-recorder T6 disambiguation
                              │
                              ▼
        effective_chain_delay = raw_wall_time − NMEA_UTC

## NMEA precision and pairing

We do **not** care about sub-second precision in the NMEA timestamp —
the LB-1421's $GNRMC time field carries the time of *sentence
emission*, typically 100-500 ms after the PPS edge it implicitly
references.  All that matters is the *integer second*.  As long as
NMEA arrives within 1 s of the PPS edge it describes (always true at
USB-CDC latencies), the integer-second tells us which GPS second the
PPS fired in.

The BPSK matched filter has independently measured *where in the RTP
stream* the polarity-flip occurred (sub-µs precision after chain_delay
calibration).  Combining the two:

    effective_chain_delay_ns =
        (raw_wall_time_at_edge − NMEA_integer_second_at_edge) * 1e9

This is the physical RF-path delay, derived without ever consulting
the host system clock as a timing source — strictly T5-on-host, no
chrony, no NTP.

## Operational notes

- The LB-1421 emits NMEA at 115200 baud (not the legacy 4800/9600 of
  older GPS modules).  Apply `stty raw 115200 -echo` before reading.
- The by-id symlink
  ``/dev/serial/by-id/usb-Leo_Bodnar_Electronics_LBE-1421_GPSDO_Locked_Clock_Source_*-if00``
  is stable across reboots; a udev rule in
  ``deploy/udev-lb1421-nmea.rules`` adds ``/dev/lb1421-nmea`` as a
  shorter alias.
- Sentences seen on bee1: ``$GNGGA`` (position+fix), ``$GNRMC`` (recommended
  minimum + status), ``$GNGSA``, ``$GPGSV``.  We parse $G[NP]RMC because
  it carries both time and date in one sentence and indicates fix
  validity (field 2: 'A' = active/valid, 'V' = void).

## Failure modes

- USB disconnect: read loop logs and retries every 5 s.
- Bad NMEA checksum: sentence skipped, debug-logged.
- No fix (status='V'): reading is recorded but
  ``get_latest(require_valid_fix=True)`` returns None.
- Stale reading (> max_age_s): ``get_latest`` returns None.

A T5 probe never blocks the disambig path — if T5 is unavailable for
any reason the existing T4 chronyc-tracking fallback engages
transparently.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_DEVICE = Path("/dev/lb1421-nmea")
DEFAULT_FALLBACK_DEVICE = Path("/dev/ttyACM3")
DEFAULT_BAUD = 115200

# Background reader retry delay after a transient device-open / read error.
RETRY_DELAY_S = 5.0

# Default freshness window: a reading older than this is treated as stale.
# 2.0 s = one NMEA-emission cycle (1 Hz) + generous margin for USB latency
# and scheduling jitter.
DEFAULT_MAX_AGE_S = 2.0


@dataclass(frozen=True)
class Lb1421Reading:
    """One NMEA-derived timing point from the LB-1421 GPSDO.

    ``pps_utc_sec`` is the absolute UTC second of the PPS edge that the
    NMEA sentence describes (integer-second precision; sub-second part
    discarded because it reflects sentence-emission delay, not the PPS
    edge itself).

    ``host_monotonic_at_read`` is ``time.monotonic()`` at the moment the
    NMEA line was read from the device — used to compute freshness.

    ``valid_fix`` is True iff the source RMC sentence's status field
    was 'A' (active); False for 'V' (void / no fix).
    """

    pps_utc_sec: int
    host_monotonic_at_read: float
    valid_fix: bool


def parse_rmc(line: str) -> Optional[Lb1421Reading]:
    """Parse a $G[NP]RMC NMEA sentence into a reading, or None on error.

    Returns None for malformed sentences, checksum failures, or other
    parse errors.  The caller decides whether to consume a no-fix
    reading (status='V').

    Sentence format::

        $GxRMC,HHMMSS.SS,A,LLLL.LL,N,YYYYY.YY,W,SOG,COG,DDMMYY,MV,MVD,M*CS

    Fields used:
      2: UTC time (HHMMSS.SS)
      3: status (A = valid, V = void)
      10: date (DDMMYY)
    """
    if not line.startswith("$"):
        return None
    # Strip checksum if present
    if "*" in line:
        payload, checksum_str = line[1:].split("*", 1)
        # Compute XOR of payload bytes; compare to hex checksum.
        expected = 0
        for ch in payload:
            expected ^= ord(ch)
        try:
            given = int(checksum_str.strip(), 16)
        except ValueError:
            return None
        if expected != given:
            return None
    else:
        payload = line[1:].rstrip()
    fields = payload.split(",")
    if len(fields) < 10:
        return None
    if fields[0] not in ("GPRMC", "GNRMC"):
        return None
    time_str = fields[1]   # HHMMSS.SS
    status = fields[2]     # A / V
    date_str = fields[9]   # DDMMYY
    if not time_str or not date_str:
        return None
    valid_fix = (status == "A")
    try:
        hh = int(time_str[0:2])
        mm = int(time_str[2:4])
        ss = int(time_str[4:6])
        dd = int(date_str[0:2])
        mo = int(date_str[2:4])
        yy = int(date_str[4:6])
        # NMEA 2-digit years roll over at 2000; for any plausible future
        # use, year >= 2000.
        year = 2000 + yy
        dt = datetime(year, mo, dd, hh, mm, ss, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None
    pps_utc_sec = int(dt.timestamp())
    return Lb1421Reading(
        pps_utc_sec=pps_utc_sec,
        host_monotonic_at_read=time.monotonic(),
        valid_fix=valid_fix,
    )


class Lb1421T5Probe:
    """Background reader of LB-1421 NMEA over USB-CDC.

    Construct with a device path (default ``/dev/lb1421-nmea``).  Call
    ``start()`` to begin the reader thread; consumers ask for the
    most-recent reading via ``get_latest()``.

    The reader thread is a daemon; ``stop()`` is best-effort.
    """

    def __init__(
        self,
        device: Path = DEFAULT_DEVICE,
        *,
        fallback_device: Path = DEFAULT_FALLBACK_DEVICE,
        baud: int = DEFAULT_BAUD,
    ) -> None:
        self.device = device
        self.fallback_device = fallback_device
        self.baud = baud
        self._latest: Optional[Lb1421Reading] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Spawn the reader thread.  Idempotent — safe to call twice."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._read_loop,
            name="lb1421-nmea-reader",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"Lb1421T5Probe: started reader thread (device={self.device}, "
            f"fallback={self.fallback_device}, baud={self.baud})"
        )

    def stop(self) -> None:
        """Signal the reader thread to exit and wait briefly."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def get_latest(
        self,
        *,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        require_valid_fix: bool = True,
    ) -> Optional[Lb1421Reading]:
        """Return the most-recent NMEA reading, or None if unavailable.

        Returns None when:
          - no reading has been received yet,
          - the most-recent reading is older than ``max_age_s``,
          - ``require_valid_fix`` is True and the reading was status='V'.
        """
        with self._lock:
            reading = self._latest
        if reading is None:
            return None
        age = time.monotonic() - reading.host_monotonic_at_read
        if age > max_age_s:
            return None
        if require_valid_fix and not reading.valid_fix:
            return None
        return reading

    def _open_device(self) -> Optional[object]:
        """Open the configured device, falling back if the by-id symlink
        is absent.  Returns a file-like object or None on failure.
        """
        for path in (self.device, self.fallback_device):
            try:
                # Open in binary mode line-buffered; NMEA is ASCII but
                # binary read is more robust against occasional non-text
                # bytes during enumeration.
                f = open(str(path), "rb", buffering=0)
                logger.info(f"Lb1421T5Probe: opened {path}")
                return f
            except FileNotFoundError:
                logger.debug(f"Lb1421T5Probe: {path} not present")
                continue
            except OSError as exc:
                logger.warning(f"Lb1421T5Probe: open({path}) failed: {exc}")
                continue
        return None

    def _read_loop(self) -> None:
        """Reader thread main loop.  Reads NMEA lines, parses, updates
        ``self._latest`` under lock.  Reopens on any I/O error.
        """
        f = None
        partial = b""
        while not self._stop.is_set():
            if f is None:
                f = self._open_device()
                if f is None:
                    # Wait before retrying so we don't busy-loop.
                    self._stop.wait(RETRY_DELAY_S)
                    continue
            try:
                chunk = f.read(256)
            except OSError as exc:
                logger.warning(f"Lb1421T5Probe: read failed: {exc}; reopening")
                try:
                    f.close()
                except OSError:
                    pass
                f = None
                self._stop.wait(RETRY_DELAY_S)
                continue
            if not chunk:
                # EOF (device gone) — reopen.
                try:
                    f.close()
                except OSError:
                    pass
                f = None
                self._stop.wait(RETRY_DELAY_S)
                continue
            partial += chunk
            while b"\n" in partial:
                line, partial = partial.split(b"\n", 1)
                line_str = line.decode("ascii", errors="replace").strip()
                if not line_str.startswith("$"):
                    continue
                reading = parse_rmc(line_str)
                if reading is None:
                    continue
                with self._lock:
                    self._latest = reading
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
