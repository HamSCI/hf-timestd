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
    written = doc.get("written_utc")
    try:
        age = now - float(written)
    except (TypeError, ValueError):
        age = None
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
