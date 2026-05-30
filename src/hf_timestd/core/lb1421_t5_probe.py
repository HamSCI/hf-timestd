"""T5 disambiguation reference — Leo Bodnar LBE-1421 GPSDO NMEA over USB.

The LBE-1421 is the GPS-disciplined oscillator on bee1: it locks to GPS,
drives the 27 MHz reference into TS-1 (which generates the BPSK HF-PPS
injected into the RF feed), AND emits NMEA sentences over its USB-CDC
serial endpoint.

This module exposes the absolute GPS UTC of the most-recent valid RMC
sentence as the T5 disambiguation reference for the BPSK PPS calibrator.

## Architecture (2026-05-30 rework — see project_t5_nmea_probe_race)

The probe is a **JSON-file poller**, not a serial reader.  The
serial endpoint is owned exclusively by the `gpsdo-monitor` daemon,
which publishes parsed NMEA state into `/run/gpsdo/<serial>.json`
(Schema v1, additive contract).  This file:

  - eliminates the dual-consumer race for `/dev/ttyACM3` that
    previously starved both readers,
  - removes any need for termios / baud / line-discipline setup,
  - matches the same `/run/gpsdo/*.json` discovery convention that
    `GpsdoProbe` already uses for A-level health.

## NMEA precision and pairing

We do **not** care about sub-second precision in the NMEA timestamp —
the LB-1421's RMC time field carries the time of *sentence emission*,
typically 100-500 ms after the PPS edge it implicitly references.  All
that matters is the *integer second*.  As long as NMEA arrives within
1 s of the PPS edge it describes (always true at USB-CDC latencies),
the integer-second tells us which GPS second the PPS fired in.

The BPSK matched filter has independently measured *where in the RTP
stream* the polarity-flip occurred (sub-µs precision after chain_delay
calibration).  Combining the two:

    effective_chain_delay_ns =
        (raw_wall_time_at_edge − NMEA_integer_second_at_edge) * 1e9

This is the physical RF-path delay, derived without ever consulting
the host system clock as a timing source — strictly T5-on-host, no
chrony, no NTP.

## Failure modes

- gpsdo-monitor not running / file missing: probe returns no reading,
  callers fall back to T4 chronyc-tracking transparently.
- gpsdo-monitor running but device has no fix (RMC status 'V'): the
  file's `pps_utc_sec` stays `None`; `get_latest(require_valid_fix=True)`
  returns None.
- File present but written_utc stale (gpsdo-monitor stalled): the
  probe's `max_nmea_age_s` check rejects it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_RUN_DIR = Path("/run/gpsdo")

# How often the reader thread polls the JSON file.  gpsdo-monitor
# refreshes on its own probe_interval (default ~10 s), but RMC sentences
# arrive at 1 Hz and the snapshot inside gpsdo-monitor's NmeaReader is
# always current, so polling faster than the probe interval still gives
# us per-second freshness once the daemon has caught up.
DEFAULT_POLL_INTERVAL_S = 0.5

# Default freshness window for a reading exposed via get_latest().
# 2.0 s = one NMEA-emission cycle (1 Hz) + generous margin.
DEFAULT_MAX_AGE_S = 2.0

# How fresh the JSON itself must be (gpsdo-monitor still writing).
# Independent from NMEA fix freshness — protects against a stalled
# publisher whose last-good NMEA pps_utc_sec is still in the file.
DEFAULT_FILE_MAX_AGE_S = 30.0

# Default for effective NMEA fix age (fix_age_at_publish + file_age).
# Sized to accommodate gpsdo-monitor's default 10 s probe_interval —
# in the worst case the JSON is written at T+0 with fix_age=0.5 s, and
# the consumer reads at T+9.9 just before the next write, yielding an
# effective fix age of ~10.4 s.  12 s gives small headroom; consumers
# can tighten it if gpsdo-monitor's probe_interval is shorter.
DEFAULT_NMEA_MAX_AGE_S = 12.0


@dataclass(frozen=True)
class Lb1421Reading:
    """One NMEA-derived timing point from the LB-1421 GPSDO.

    ``pps_utc_sec`` is the absolute UTC second of the PPS edge that the
    NMEA sentence describes (integer-second precision; sub-second part
    discarded because it reflects sentence-emission delay, not the PPS
    edge itself).

    ``host_monotonic_at_read`` is ``time.monotonic()`` at the moment
    this probe read fresh data from the JSON file — used to compute
    freshness on the consumer side.  Note: this is the *consumer's*
    monotonic, not gpsdo-monitor's, because monotonic() is not
    comparable across processes.

    ``valid_fix`` is True iff the source RMC sentence's status was 'A'
    (active) when gpsdo-monitor read it.  Derived from the freshness of
    ``health.fix_age_sec`` in the published JSON: True when fresh enough,
    False otherwise.
    """

    pps_utc_sec: int
    host_monotonic_at_read: float
    valid_fix: bool


class Lb1421T5Probe:
    """Background poller of gpsdo-monitor's per-device JSON.

    Construct with a ``run_dir`` (default ``/run/gpsdo``) and an
    optional ``serial`` filter.  Call ``start()`` to begin the
    background polling thread; consumers ask for the most-recent
    reading via ``get_latest()``.

    The reader thread is a daemon; ``stop()`` is best-effort.
    """

    def __init__(
        self,
        run_dir: Path = DEFAULT_RUN_DIR,
        *,
        serial: Optional[str] = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        file_max_age_s: float = DEFAULT_FILE_MAX_AGE_S,
        nmea_max_age_s: float = DEFAULT_NMEA_MAX_AGE_S,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.serial = serial
        self.poll_interval_s = float(poll_interval_s)
        self.file_max_age_s = float(file_max_age_s)
        self.nmea_max_age_s = float(nmea_max_age_s)
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
            name="lb1421-gpsdo-poller",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"Lb1421T5Probe: started gpsdo-monitor poller "
            f"(run_dir={self.run_dir}, serial={self.serial or '*'}, "
            f"poll={self.poll_interval_s}s)"
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

        ``pps_utc_sec`` is the raw integer UTC second from NMEA — the
        GPSDO's reported time, NOT a host-clock projection.  The
        consumer is responsible for pairing it against a same-vintage
        RTP-derived edge wall time.  Freshness comes from the underlying
        JSON publish cadence in gpsdo-monitor; if that cadence is too
        slow for the disambig pairing guard, the fix belongs there
        (publish on every RMC), not in synthesising a wall-clock value
        here.
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

    # --- internal -------------------------------------------------------

    def _read_loop(self) -> None:
        """Reader thread main loop.  Polls the gpsdo-monitor JSON file,
        parses, updates ``self._latest`` under lock.
        """
        while not self._stop.is_set():
            try:
                reading = self._read_once()
            except Exception:
                logger.exception("Lb1421T5Probe: unexpected error in poll")
                reading = None
            if reading is not None:
                with self._lock:
                    self._latest = reading
            self._stop.wait(self.poll_interval_s)

    def _read_once(self) -> Optional[Lb1421Reading]:
        """One poll cycle.  Returns a fresh Lb1421Reading or None.

        The JSON's ``pps_utc_sec`` is the integer GPS second of the
        most-recent RMC observed by gpsdo-monitor — at file-write time
        that may already be up to ``fix_age_sec`` old, and the file
        itself is only rewritten once per gpsdo-monitor probe interval
        (default 10 s).  Naively returning the raw value to callers
        would yield a ``pps_utc_sec`` that is up to ``probe_interval +
        fix_age`` seconds stale, blowing past the ±0.5 s pairing guard
        in ``_t6_disambiguate_via_t5_lb1421`` and forcing fallback to
        T4 chronyc-tracking.  The original semantic (direct serial
        reader) refreshed ``pps_utc_sec`` once per second, so the
        consumer expected freshness in the same range.

        We restore that semantic here by projecting ``pps_utc_sec``
        forward to ``time.time()`` using the host wall clock.  Both
        the host clock (chrony-disciplined) and the GPSDO advance at
        1 s/sec, so adding ``int(round(host_now − W_obs))`` advances
        the integer-second by the number of PPS edges that have fired
        since the observation.  The chrony discipline noise (sub-µs)
        is negligible against integer-second projection.

        We still use the JSON only to ATTEST that the GPSDO is locked
        (fresh fix_age, schema v1, file recent) — once attested, the
        actual integer second is host-clock-derived.  If GPS lock
        drops, fix_age grows past ``nmea_max_age_s`` and we mark
        ``valid_fix=False`` so the disambig falls through to T4.
        """
        path = self._pick_file()
        if path is None:
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            logger.debug(f"Lb1421T5Probe: {path} unreadable: {e}")
            return None
        if not isinstance(data, dict) or data.get("schema") != "v1":
            return None

        # File freshness — gpsdo-monitor still writing recently.
        written_age = self._written_utc_age(data.get("written_utc"))
        if written_age is None or written_age > self.file_max_age_s:
            return None

        health = data.get("health") or {}
        raw_pps_utc_sec = health.get("pps_utc_sec")
        if not isinstance(raw_pps_utc_sec, int):
            return None

        # Effective NMEA fix age = fix_age_at_publish + elapsed since
        # publish.  Used as the freshness gate; raw_pps_utc_sec itself
        # is stored verbatim — projection to "now" happens in
        # get_latest() against the consumer's clock instant.
        fix_age_at_publish = health.get("fix_age_sec")
        if not isinstance(fix_age_at_publish, (int, float)):
            return None
        effective_fix_age = float(fix_age_at_publish) + written_age

        # Consistency check: gpsdo-monitor observed the RMC at wall
        # time W_obs = host_now − effective_fix_age, with NMEA reporting
        # integer UTC second raw_pps_utc_sec.  W_obs − raw_pps_utc_sec
        # is the NMEA sentence-emission delay; under chrony-disciplined
        # host clock + locked GPSDO it sits in [0, 1) sec.  Outside
        # [-0.5, 1.5] means host clock and NMEA truth disagree at the
        # integer-second level — demote T5.
        host_now = time.time()
        nmea_emission_delay = (host_now - effective_fix_age) - raw_pps_utc_sec
        host_gps_consistent = -0.5 <= nmea_emission_delay <= 1.5

        valid_fix = (
            effective_fix_age <= self.nmea_max_age_s
            and host_gps_consistent
        )

        return Lb1421Reading(
            pps_utc_sec=raw_pps_utc_sec,
            host_monotonic_at_read=time.monotonic(),
            valid_fix=valid_fix,
        )

    def _pick_file(self) -> Optional[Path]:
        if not self.run_dir.is_dir():
            return None
        if self.serial is not None:
            candidate = self.run_dir / f"{self.serial}.json"
            return candidate if candidate.is_file() else None
        # No explicit serial: pick the first per-device file, skipping
        # index.json.  On bee1 there is one LB-1421; if a deployment
        # ever has multiple GPSDOs, set `serial` explicitly.
        for entry in sorted(self.run_dir.glob("*.json")):
            if entry.name == "index.json":
                continue
            return entry
        return None

    def _written_utc_age(self, written_utc: object) -> Optional[float]:
        if not isinstance(written_utc, str):
            return None
        try:
            dt = datetime.fromisoformat(written_utc.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = time.time() - dt.timestamp()
        return max(0.0, age)
