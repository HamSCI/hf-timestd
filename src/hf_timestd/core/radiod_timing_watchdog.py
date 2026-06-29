"""RadiodTimingWatchdog — capture decisive evidence on a gross RTP↔UTC jump.

radiod maps RTP timestamps to UTC via a published ``(gps_time, rtp_timesnap)``
pair.  Two regimes of "slide" exist:

  * The everyday slide — sub-second drift plus ~0.45 s status-pair jitter/tearing.
    Normal; clients accommodate it (slide-follow).  NOT an incident.

  * A gross thrash — the mapping jumping by *seconds to hundreds of seconds*
    (observed 2026-06-29: −489/−252/+861 s, with a 2016-era ``gps_time``),
    severe enough to break decoders fleet-wide until radiod was restarted.

When the gross thrash recurs we want a one-shot, self-documenting evidence
bundle that answers the only question that matters: **is the GPS time SOURCE
bad (e.g. a receiver week-rollover — fix the hardware) or is radiod computing
the mapping wrong from a good source (a candidate ka9q-radio issue worth raising
upstream)?**  This watchdog is fired by the archive writer's existing
"RTP counter space CHANGED" detector; on a jump beyond ``BIG_JUMP_SEC`` it reads
gpsd and chrony, compares their epochs against radiod's, writes a verdict, and
alarms.

Timing-authority invariant (METROLOGY §4.5): the gpsd / chrony / ``time.time()``
reads here are **diagnostic only** — used solely to classify a fault, never fed
into the timing path or the chrony feed.  The watchdog never influences the
RTP→UTC mapping; it only observes, classifies, persists, and logs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Jumps below this are the ordinary re-anchor / status-jitter regime (the
# archive detector trips at ~0.25 s, normal jitter is ~0.45 s).  The thrash is
# seconds-to-minutes, so this threshold cleanly separates the two and never
# false-fires on jitter.
BIG_JUMP_SEC = 2.0

# One evidence bundle per thrash episode — a sustained thrash trips the detector
# many times per minute; capture once and count the rest.
INCIDENT_COOLDOWN_SEC = 60.0

# An epoch this far from the system clock is "insane" (a GPS week-rollover lands
# ~10–20 years off; a torn/garbage pair lands wildly off).  Sub-day offsets are
# treated as a same-epoch mapping slip, not an epoch fault.
EPOCH_INSANE_SEC = 86400.0

_STATUS_DIR = Path(os.environ.get("TIMESTD_STATUS_DIR", "/var/lib/timestd/status"))
INCIDENTS_PATH = _STATUS_DIR / "radiod-timing-incidents.jsonl"
STATUS_PATH = _STATUS_DIR / "radiod-timing-watchdog.json"  # latest verdict, read by smd


@dataclass
class _ExternalClocks:
    """A diagnostic snapshot of the host's external time references."""
    gpsd_unix: Optional[float] = None
    gpsd_mode: Optional[int] = None        # 1=no fix, 2=2D, 3=3D
    gpsd_epoch_off_sec: Optional[float] = None
    chrony_refname: Optional[str] = None
    chrony_stratum: Optional[int] = None
    chrony_offset_sec: Optional[float] = None
    chrony_leap: Optional[str] = None
    errors: list = field(default_factory=list)


def _read_gpsd(system_now: float) -> _ExternalClocks:
    """Read gpsd's current fix epoch via ``gpspipe -w``.  Diagnostic only."""
    ec = _ExternalClocks()
    try:
        out = subprocess.run(
            ["gpspipe", "-w", "-n", "8"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except FileNotFoundError:
        ec.errors.append("gpspipe-not-found")
        return ec
    except (subprocess.TimeoutExpired, OSError) as exc:
        ec.errors.append(f"gpspipe-failed:{exc}")
        return ec
    for line in out.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("class") == "TPV" and obj.get("time"):
            try:
                t = datetime.fromisoformat(
                    obj["time"].replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                continue
            ec.gpsd_unix = t
            ec.gpsd_mode = obj.get("mode")
            ec.gpsd_epoch_off_sec = t - system_now
    if ec.gpsd_unix is None and not ec.errors:
        ec.errors.append("gpsd-no-TPV-time")
    return ec


def _read_chrony(ec: _ExternalClocks) -> None:
    """Read ``chronyc -c tracking`` (CSV).  Diagnostic only."""
    try:
        out = subprocess.run(
            ["chronyc", "-c", "tracking"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except FileNotFoundError:
        ec.errors.append("chronyc-not-found")
        return
    except (subprocess.TimeoutExpired, OSError) as exc:
        ec.errors.append(f"chronyc-failed:{exc}")
        return
    # CSV fields: refid, refname, stratum, reftime, sys_offset, last_offset,
    #   rms_offset, freq_ppm, resid_ppm, skew, root_delay, root_disp,
    #   update_interval, leap
    f = out.split(",")
    if len(f) >= 14:
        ec.chrony_refname = f[1] or None
        try:
            ec.chrony_stratum = int(f[2])
        except ValueError:
            pass
        try:
            ec.chrony_offset_sec = float(f[4])
        except ValueError:
            pass
        ec.chrony_leap = f[13] or None
    else:
        ec.errors.append("chronyc-unparsed")


def _verdict(delta_sec: float, radiod_utc: float, system_now: float,
             ec: _ExternalClocks) -> tuple:
    """Classify the jump → (code, severity, human).

    severity ∈ {"warn", "fail"}.  The verdict isolates GPS-source-bad from
    radiod-bad so the operator knows whether to fix hardware or raise an
    upstream ka9q-radio issue.
    """
    gps_off = ec.gpsd_epoch_off_sec
    radiod_off = radiod_utc - system_now

    if gps_off is not None and abs(gps_off) > EPOCH_INSANE_SEC:
        return (
            "GPS_SOURCE_BAD_EPOCH", "fail",
            f"gpsd reports an epoch {abs(gps_off) / 86400:.0f} days off "
            f"(week-rollover / bad almanac). radiod faithfully reported the "
            f"source time — this is a GPS RECEIVER fault, not ka9q-radio. "
            f"Fix/replace the GPS receiver, then restart radiod.",
        )

    if abs(radiod_off) > EPOCH_INSANE_SEC:
        if gps_off is not None and abs(gps_off) < 60.0:
            return (
                "RADIOD_BAD_EPOCH_GOOD_SOURCE", "fail",
                f"radiod's gps_time is {abs(radiod_off) / 86400:.0f} days off "
                f"while gpsd's epoch is sane (off {gps_off:+.1f}s) — radiod is "
                f"computing the mapping wrong from a good source. CANDIDATE "
                f"ka9q-radio issue: capture this bundle for Phil Karn.",
            )
        return (
            "RADIOD_BAD_EPOCH_SOURCE_UNKNOWN", "fail",
            f"radiod's gps_time is {abs(radiod_off) / 86400:.0f} days off and "
            f"gpsd could not be read ({', '.join(ec.errors) or 'no fix'}) — "
            f"can't yet isolate source-vs-radiod; check the GPS receiver first.",
        )

    gps_note = (
        f"gpsd epoch sane (off {gps_off:+.1f}s)" if gps_off is not None
        else "gpsd unread"
    )
    return (
        "MAPPING_JUMP", "warn",
        f"radiod RTP↔UTC jumped {delta_sec:+.1f}s with sane epochs "
        f"({gps_note}) — transient status tear or a genuine seconds-level "
        f"slip; monitor and escalate only if recurrent.",
    )


class RadiodTimingWatchdog:
    """Host-wide (singleton) watchdog: one evidence bundle per thrash episode."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_incident_mono: float = -1e18
        self._suppressed_since_last: int = 0

    def on_mapping_jump(self, *, channel: str, gps_time_ns: int,
                        rtp_timesnap: int, radiod_utc: float, old_utc: float,
                        delta_sec: float) -> None:
        """Called by the archive detector on a non-wrap 'RTP counter space
        CHANGED'.  Cheap and re-entrant; only large, cooled-down jumps capture
        evidence.  Never raises (the caller is on the recording path)."""
        try:
            if abs(delta_sec) < BIG_JUMP_SEC:
                return  # ordinary re-anchor / jitter — not an incident
            now_mono = time.monotonic()
            with self._lock:
                if now_mono - self._last_incident_mono < INCIDENT_COOLDOWN_SEC:
                    self._suppressed_since_last += 1
                    return
                suppressed = self._suppressed_since_last
                self._suppressed_since_last = 0
                self._last_incident_mono = now_mono
            self._capture(channel, gps_time_ns, rtp_timesnap, radiod_utc,
                          old_utc, delta_sec, suppressed)
        except Exception:  # noqa: BLE001 — must never disturb recording
            logger.exception("radiod timing watchdog: capture failed")

    def _capture(self, channel, gps_time_ns, rtp_timesnap, radiod_utc, old_utc,
                 delta_sec, suppressed) -> None:
        system_now = time.time()
        ec = _read_gpsd(system_now)
        _read_chrony(ec)
        code, severity, human = _verdict(delta_sec, radiod_utc, system_now, ec)

        incident = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "verdict": code,
            "severity": severity,
            "detail": human,
            "channel": channel,
            "delta_sec": round(delta_sec, 3),
            "suppressed_repeats": suppressed,
            "radiod": {
                "gps_time_ns": gps_time_ns,
                "rtp_timesnap": rtp_timesnap,
                "implied_utc": round(radiod_utc, 3),
                "old_mapping_utc": round(old_utc, 3),
                "epoch_off_sec": round(radiod_utc - system_now, 3),
            },
            "external": asdict(ec),
            "system_unix": round(system_now, 3),
        }

        logger.error(
            "RADIOD TIMING INCIDENT [%s/%s] %s — %s", severity.upper(), code,
            f"{channel} jumped {delta_sec:+.1f}s", human,
        )
        self._persist(incident)

    @staticmethod
    def _persist(incident: dict) -> None:
        try:
            _STATUS_DIR.mkdir(parents=True, exist_ok=True)
            with open(INCIDENTS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(incident) + "\n")
            tmp = STATUS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(incident, indent=2))
            tmp.replace(STATUS_PATH)  # atomic latest-verdict for smd
        except OSError as exc:
            logger.warning("radiod timing watchdog: could not persist incident: %s", exc)


_WATCHDOG: Optional[RadiodTimingWatchdog] = None
_WATCHDOG_LOCK = threading.Lock()


def get_watchdog() -> RadiodTimingWatchdog:
    """Process-wide singleton (one incident stream per host)."""
    global _WATCHDOG
    if _WATCHDOG is None:
        with _WATCHDOG_LOCK:
            if _WATCHDOG is None:
                _WATCHDOG = RadiodTimingWatchdog()
    return _WATCHDOG
