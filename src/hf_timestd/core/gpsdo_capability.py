"""What the attached GPSDO can actually do, read from gpsdo-monitor.

A GPSDO's primary job is the **A axis**: it disciplines the ADC's clock, which
is what keeps a ruler good over time.  Every supported model does that.  Only
*some* of them also serve the **T axis** — naming and placing the second — and
only those are T5 sources.  ``METROLOGY.md`` states the prerequisite literally:
"A1 + LBE-1421 USB connected to host", and notes that "the T5 definition is the
USB transport".

Concretely, from gpsdo-monitor's own drivers:

    lbe-1421/1423   has_pps=True,  has_nmea_cdc=True    places a second  -> T5
    lbe-mini        has_pps=False, has_ubx_hid=True     names a second   -> not T5
    lbe-1420/1425   neither declared                    A axis only

## Why this module exists

``[timing] lb1421_enabled`` was a hand-set boolean named after one model.
AC0G-ND carried it ``true`` for its entire life against an LBE-Mini, which has
no PPS, so the T5 probe reported ``enabled: true, valid_fix: false,
"no reading yet"`` forever — and every surface read that as "T5 is on".  It was
never going to yield a reading, because a Mini cannot place a second.

Meanwhile gpsdo-monitor was already publishing the discriminator, on both
stations, in ``/run/gpsdo/<serial>.json``:

    B4   lbe-1421   pps_study {enabled: True,  edges: 60, period_ms_p50: 1000.084}
    ND   lbe-mini   pps_study {enabled: False, edges: 0}

So the capability is knowable without asking the operator, and asking the
operator is what produced a station that looked configured and was not.

⚠ ``pps_study.enabled`` is a *setting*; ``edges`` is *evidence*.  This module
requires the evidence.  Believing a setting over a measurement is the exact
shape of the bug it replaces.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

#: A published document older than this is not evidence of anything current.
#: gpsdo-monitor writes on its probe interval (seconds); minutes of silence
#: means the daemon stalled, and a stale "yes" must never read as a live one.
MAX_DOC_AGE_S = 300.0

DEFAULT_RUN_DIR = Path("/run/gpsdo")


def _doc_age_s(written, now: float) -> Optional[float]:
    """Seconds since the document was written, or None if unreadable.

    ⚠ gpsdo-monitor writes ``written_utc`` as an ISO-8601 STRING
    (``'2026-09-17T00:07:34.025Z'``), not an epoch number.  The first cut of
    this module assumed a float, so every real document parsed as "age
    unknown" and was rejected as stale — a station with a perfectly fresh
    probe reported no T5 for the wrong reason.  It passed its tests because
    the fixtures were invented rather than captured.  Numbers are still
    accepted: cheap, and it costs nothing to read both.
    """
    if written is None:
        return None
    if isinstance(written, (int, float)):
        return now - float(written)
    try:
        from datetime import datetime, timezone
        s = str(written).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return now - dt.timestamp()
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class T5Capability:
    """Whether the attached device can serve as a T5 source, and why.

    ``reason`` is always populated, including on success, because a bare
    boolean cannot tell an operator whether T5 is off because the hardware
    cannot do it, because the daemon is stalled, or because nobody plugged
    anything in.  Those need different actions.
    """

    available: bool
    reason: str
    model: Optional[str] = None
    #: True when the device reports a GPS fix but cannot place a second.
    #: Not T5 — but it is what T6 needs for second-of-day disambiguation,
    #: so it is surfaced rather than collapsed into a plain "no".
    names_second: bool = False
    probe_present: bool = False


def resolve_t5_capability(doc: Optional[dict], *, now: Optional[float] = None,
                          max_age_s: float = MAX_DOC_AGE_S) -> T5Capability:
    """Derive T5 eligibility from one gpsdo-monitor device document."""
    now = time.time() if now is None else float(now)

    if not isinstance(doc, dict):
        return T5Capability(
            False, "no gpsdo-monitor document found (daemon not running, or no device)",
            probe_present=False)

    model = ((doc.get("device") or {}).get("model")) or None
    age = _doc_age_s(doc.get("written_utc"), now)
    if age is None or age > max_age_s:
        shown = "unknown" if age is None else f"{age:.0f}s"
        return T5Capability(
            False, f"gpsdo-monitor document is stale (age {shown}) — not current evidence",
            model=model, probe_present=True)

    health = doc.get("health") or {}
    has_fix = bool(health.get("gps_fix"))

    study = doc.get("pps_study") or {}
    try:
        edges = int(study.get("edges") or 0)
    except (TypeError, ValueError):
        edges = 0

    if study.get("enabled") and edges > 0:
        return T5Capability(
            True, f"pps measured: {edges} edge(s) in the study window",
            model=model, names_second=has_fix, probe_present=True)

    # No PPS. Distinguish "cannot" from "can name but not place", because the
    # second case is a usable second-naming source for T6 and the first is not.
    if study.get("enabled"):
        why = "pps study enabled but no edges measured"
    else:
        why = "device reports no pps"
    return T5Capability(
        False, why, model=model, names_second=has_fix, probe_present=True)


def load_device_doc(run_dir: Path = DEFAULT_RUN_DIR,
                    serial: Optional[str] = None) -> Optional[dict]:
    """The per-device document, or None. Never raises: callers are startup paths."""
    try:
        if serial:
            p = Path(run_dir) / f"{serial}.json"
            return json.loads(p.read_text()) if p.exists() else None
        best = None
        for p in sorted(Path(run_dir).glob("*.json")):
            if p.name == "index.json":
                continue
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            # Prefer a PPS-capable device when a host has more than one.
            if best is None or (d.get("pps_study") or {}).get("enabled"):
                best = d
        return best
    except OSError:
        return None


def resolve_t5_for_config(timing_cfg: dict) -> Tuple[bool, str, T5Capability,
                                                     Path, Optional[str]]:
    """One answer for every caller: should T5 run, why, and with what.

    ⚠ This exists because the enable logic was DUPLICATED — once in
    ``cli.py``'s ``daemon`` command and once in ``core_recorder_v2``'s own
    ``__main__``.  The deployed systemd unit runs the module
    (``python -m hf_timestd.core.core_recorder_v2``), not the CLI, so a fix
    applied only to ``cli.py`` is dead code in production.  That is exactly how
    this was nearly shipped on 2026-09-16: the derivation went into the CLI, the
    station restarted, and nothing changed because the unit never calls it.

    Returns ``(enabled, why, capability, run_dir, serial)``.
    """
    run_dir = Path(str(timing_cfg.get('lb1421_gpsdo_run_dir',
                                      str(DEFAULT_RUN_DIR))))
    serial = timing_cfg.get('lb1421_gpsdo_serial') or None
    cap = resolve_t5_capability(load_device_doc(run_dir, serial))
    enabled, why = t5_enabled_from_config(timing_cfg, cap)
    return enabled, why, cap, run_dir, serial


def t5_enabled_from_config(timing_cfg: dict,
                           cap: T5Capability) -> Tuple[bool, str]:
    """Resolve the T5 switch: explicit config first, otherwise the probe.

    Precedence, and the reasoning for each step:

    1. **Explicit off wins always.** Disabling T5 on a capable station is a
       legitimate operator choice and nothing here should override it.
    2. **Explicit on is REFUSED against a device known to lack PPS.** This is
       ND's actual state for its whole life. Obeying it produced a T5 that
       reported enabled and never yielded a reading; refusing it says why.
    3. **Explicit on with no probe is obeyed.** A missing gpsdo-monitor is not
       evidence of missing hardware, and refusing here would break every
       station that does not run the daemon.
    4. **Unset follows the probe**, which is the point of the module.
    """
    explicit = timing_cfg.get("t5_enabled")
    if explicit is None:
        # `lb1421_enabled` is the deployed spelling on live stations.
        explicit = timing_cfg.get("lb1421_enabled")
    if explicit is None and str(timing_cfg.get("lb1421_nmea_device", "")).strip():
        explicit = True  # legacy enable-signal

    if explicit is False:
        return False, "disabled explicitly in config"
    if explicit is True:
        if cap.probe_present and not cap.available:
            return False, (
                f"config asks for T5 but the attached device cannot provide it "
                f"({cap.reason}"
                + (f", model {cap.model}" if cap.model else "") + ")")
        return True, "enabled explicitly in config"
    return cap.available, cap.reason


def attach_second_namer(recorder, run_dir, serial, logger=None) -> bool:
    """Wire a names-but-cannot-place device into T6's second naming.

    Call when :func:`resolve_t5_capability` reports ``available=False`` with
    ``names_second=True`` — an LBE-mini, typically: GPS time-of-day over USB,
    no PPS at all.

    ## Why this is separate from the T5 probe

    "Which integer second is this?" needs ±0.5 s.  "Where exactly is the
    second boundary?" needs microseconds and a PPS.  Both lived behind
    ``lb1421_enabled``, so a station that could answer the first refused to,
    and published ``naming_unavailable`` while the answer sat on its USB bus.
    Measured on DASI-009.AI6VN 2026-09-18: T6 found the edge and could not
    name its second, with no HF antenna (no T3) and no LAN GPS (no T4) to ask.

    ⛔ It must NOT go to ``attach_lb1421_probe``.  That slot also lights the
    T5 bench — ``_t5_lbe1421_product``'s docstring says "lb1421_enabled=true
    alone ... is sufficient to light the T5 bench" — so a PPS-less device
    there reports a T5 that never yields a reading, which is precisely the ND
    defect this module exists to have removed.

    ⚠ Inert until gpsdo-monitor publishes ``pps_utc_sec`` for the device.
    Today an LBE-mini's document carries ``pps_utc_sec: null``, so the probe
    yields no reading and naming falls through exactly as before.  Shipping
    the consumer first is safe — it degrades to today's behaviour — but it
    changes nothing on its own, and must not be reported as a fix.

    Lives here rather than in either entry point because the T5 enable logic
    was once duplicated across ``cli.py`` and ``core_recorder_v2.__main__``,
    and a fix applied to only one of them was dead code in production.

    Returns True when a namer was attached.  Never raises: startup path.
    """
    try:
        from .lb1421_t5_probe import Lb1421T5Probe
        probe = Lb1421T5Probe(run_dir=run_dir, serial=serial)
        probe.start()
        recorder.attach_second_namer(probe)
        if logger is not None:
            logger.info(
                "T6 second-namer attached (gpsdo run_dir=%s, serial=%s): this "
                "device names a second but does not place one, so it feeds "
                "T6 disambiguation ONLY and does not light T5. Inert until "
                "gpsdo-monitor publishes pps_utc_sec for it.",
                run_dir, serial or '*')
        return True
    except Exception as exc:                                   # noqa: BLE001
        if logger is not None:
            logger.warning("T6 second-namer could not be attached: %s", exc)
        return False
